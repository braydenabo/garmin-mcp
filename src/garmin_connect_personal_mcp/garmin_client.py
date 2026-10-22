"""The only module that talks to Garmin.

A thin layer over Garmin's official client that adds what it leaves to callers:
retries with backoff on rate limits, server errors and network failures; one
token refresh on a 401; and errors that say what to do next. Garmin's response
bodies are kept out of error messages, since they can echo personal data.
"""

import random
import time
from collections.abc import Callable
from typing import Protocol, TypeVar

import requests
from garmin_personal_api import (
    BearerToken,
    GarminPersonalApiClient,
    GarminPersonalApiError,
    HttpError,
    PersonalApiAuthenticator,
    resolve_api_key,
)

from garmin_connect_personal_mcp import config

T = TypeVar("T")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GarminError(Exception):
    """A Garmin failure with a message the user (or a model) can act on."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Authenticator(Protocol):
    def obtain_token(self, api_key: str, *, force_refresh: bool = False) -> BearerToken: ...

    def close(self) -> None: ...


def describe_http_error(status: int) -> str:
    if status == 401:
        return (
            "Garmin rejected the API key (HTTP 401). It may have been revoked or expired. "
            f"Create a new key at {config.API_KEY_URL}, then run "
            "`garmin-connect-personal-mcp init --force`."
        )
    if status == 403:
        return "Garmin refused access to this data (HTTP 403)."
    if status == 404:
        return "Garmin has no data at this address (HTTP 404)."
    if status == 429:
        return "Garmin's rate limit was reached (HTTP 429). Wait a while and try again."
    if status >= 500:
        return f"Garmin's servers returned an error (HTTP {status}). Try again later."
    return f"Garmin returned HTTP {status}."


class GarminApi:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        authenticator: Authenticator | None = None,
        client_factory: Callable[[str], GarminPersonalApiClient] = GarminPersonalApiClient,
        max_retries: int = 4,
        base_delay_s: float = 2.0,
        max_delay_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max_retries = max_retries
        self._base_delay_s = base_delay_s
        self._max_delay_s = max_delay_s
        self._sleep = sleep
        try:
            self._key = resolve_api_key(api_key)
        except GarminPersonalApiError:
            raise GarminError(
                f"No Garmin API key found. Create one at {config.API_KEY_URL}, then run "
                "`garmin-connect-personal-mcp init`."
            ) from None
        self._auth = authenticator or PersonalApiAuthenticator()
        token = self._with_retries(lambda: self._auth.obtain_token(self._key))
        self._client = client_factory(token.access_token)

    def call(self, request: Callable[[GarminPersonalApiClient], T]) -> T:
        """Run one client call, e.g. `api.call(lambda c: c.get_daily_sleep(day))`."""
        try:
            return self._with_retries(lambda: request(self._client))
        except GarminError as exc:
            if exc.status_code != 401:
                raise
        # The token was rejected before it expired (e.g. revoked); get a new one once.
        token = self._with_retries(lambda: self._auth.obtain_token(self._key, force_refresh=True))
        self._client.update_bearer_token(token.access_token)
        return self._with_retries(lambda: request(self._client))

    def close(self) -> None:
        self._client.close()
        self._auth.close()

    def __enter__(self) -> "GarminApi":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _delay(self, attempt: int) -> float:
        delay = min(self._max_delay_s, self._base_delay_s * 2.0**attempt)
        return delay * (0.5 + random.random() / 2)  # noqa: S311 (jitter, not crypto)

    def _with_retries(self, action: Callable[[], T]) -> T:
        attempt = 0
        while True:
            try:
                return action()
            except HttpError as exc:
                status = exc.status_code
                if status not in RETRYABLE_STATUS or attempt >= self._max_retries:
                    raise GarminError(describe_http_error(status), status_code=status) from None
            except (requests.ConnectionError, requests.Timeout):
                if attempt >= self._max_retries:
                    raise GarminError(
                        "Couldn't reach Garmin. Check your internet connection and try again."
                    ) from None
            self._sleep(self._delay(attempt))
            attempt += 1
