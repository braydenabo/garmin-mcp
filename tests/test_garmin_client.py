"""GarminApi against Garmin's real client with a fake HTTP session (no network)."""

import json
from collections.abc import Iterable
from typing import Any

import pytest
import requests
from garmin_personal_api import BearerToken, GarminPersonalApiClient, HttpError

from garmin_connect_personal_mcp import config
from garmin_connect_personal_mcp.garmin_client import GarminApi, GarminError

Outcome = int | Exception | dict[str, Any]


def response(status: int, body: Any = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = b"" if body is None else json.dumps(body).encode()
    return r


class FakeSession(requests.Session):
    """Returns queued outcomes: an int status, an exception to raise, or a JSON body (200)."""

    def __init__(self, outcomes: Iterable[Outcome]) -> None:
        super().__init__()
        self.outcomes = list(outcomes)
        self.auth_headers: list[str] = []

    def get(self, url: str | bytes, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        self.auth_headers.append(kwargs["headers"]["Authorization"])
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            return response(outcome, {"detail": "secret-ish body for user 42"})
        return response(200, outcome)


class FakeAuth:
    def __init__(self, failures: Iterable[Exception] = ()) -> None:
        self.failures = list(failures)
        self.calls: list[bool] = []

    def obtain_token(self, api_key: str, *, force_refresh: bool = False) -> BearerToken:
        self.calls.append(force_refresh)
        if self.failures:
            raise self.failures.pop(0)
        return BearerToken(access_token=f"token-{len(self.calls)}", expires_at=0)

    def close(self) -> None:
        pass


def make_api(
    outcomes: Iterable[Outcome], auth: FakeAuth | None = None, **kwargs: Any
) -> tuple[GarminApi, FakeSession, FakeAuth, list[float]]:
    session = FakeSession(outcomes)
    auth = auth or FakeAuth()
    sleeps: list[float] = []
    api = GarminApi(
        "fake-key",
        authenticator=auth,
        client_factory=lambda token: GarminPersonalApiClient(token, session=session),
        sleep=sleeps.append,
        **kwargs,
    )
    return api, session, auth, sleeps


def http_error(status: int) -> HttpError:
    return HttpError(status_code=status, method="POST", url="u", response_text="body")


def test_success_returns_json() -> None:
    api, _, _, sleeps = make_api([{"sleepTimeSeconds": 100}])
    assert api.call(lambda c: c.get("/x")) == {"sleepTimeSeconds": 100}
    assert sleeps == []


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retries_transient_errors(status: int) -> None:
    api, _, _, sleeps = make_api([status, status, {"ok": True}])
    assert api.call(lambda c: c.get("/x")) == {"ok": True}
    assert len(sleeps) == 2


def test_retries_network_errors() -> None:
    api, _, _, sleeps = make_api([requests.ConnectionError(), requests.Timeout(), {"ok": True}])
    assert api.call(lambda c: c.get("/x")) == {"ok": True}
    assert len(sleeps) == 2


def test_backoff_grows_and_is_capped() -> None:
    api, _, _, sleeps = make_api([503] * 5, base_delay_s=2, max_delay_s=5)
    with pytest.raises(GarminError):
        api.call(lambda c: c.get("/x"))
    assert len(sleeps) == 4
    assert 1 <= sleeps[0] <= 2
    assert 2 <= sleeps[1] <= 4
    assert all(s <= 5 for s in sleeps)


def test_gives_up_with_actionable_message() -> None:
    api, _, _, _ = make_api([429] * 5)
    with pytest.raises(GarminError, match="rate limit") as exc:
        api.call(lambda c: c.get("/x"))
    assert exc.value.status_code == 429


def test_network_failure_message() -> None:
    api, _, _, _ = make_api([requests.ConnectionError()] * 5)
    with pytest.raises(GarminError, match="internet connection"):
        api.call(lambda c: c.get("/x"))


@pytest.mark.parametrize("status", [400, 403, 404])
def test_does_not_retry_client_errors(status: int) -> None:
    api, _, _, sleeps = make_api([status])
    with pytest.raises(GarminError) as exc:
        api.call(lambda c: c.get("/x"))
    assert exc.value.status_code == status
    assert sleeps == []


def test_error_hides_garmin_response_body() -> None:
    api, _, _, _ = make_api([500] * 5)
    with pytest.raises(GarminError) as exc:
        api.call(lambda c: c.get("/x"))
    assert "user 42" not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


def test_401_refreshes_token_once_then_succeeds() -> None:
    api, session, auth, _ = make_api([401, {"ok": True}])
    assert api.call(lambda c: c.get("/x")) == {"ok": True}
    assert auth.calls == [False, True]
    assert session.auth_headers == ["Bearer token-1", "Bearer token-2"]


def test_401_after_refresh_explains_how_to_fix() -> None:
    api, _, auth, _ = make_api([401, 401])
    with pytest.raises(GarminError, match="init --force") as exc:
        api.call(lambda c: c.get("/x"))
    assert exc.value.status_code == 401
    assert auth.calls == [False, True]


def test_token_exchange_is_retried() -> None:
    auth = FakeAuth([http_error(503)])
    api, _, _, sleeps = make_api([{"ok": True}], auth=auth)
    assert api.call(lambda c: c.get("/x")) == {"ok": True}
    assert len(sleeps) == 1


def test_rejected_key_at_startup() -> None:
    with pytest.raises(GarminError, match="init --force"):
        make_api([], auth=FakeAuth([http_error(401)]))


def test_missing_key_points_to_init() -> None:
    with pytest.raises(GarminError, match="garmin-connect-personal-mcp init"):
        GarminApi(authenticator=FakeAuth())


def test_context_manager_closes_session_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    api, session, auth, _ = make_api([])
    closed: list[str] = []
    monkeypatch.setattr(session, "close", lambda: closed.append("session"))
    monkeypatch.setattr(auth, "close", lambda: closed.append("auth"))
    with api:
        pass
    assert closed == ["session", "auth"]


def test_finds_key_saved_by_init() -> None:
    config.save_api_key("fake-key-123")
    auth = FakeAuth()
    GarminApi(authenticator=auth, client_factory=lambda t: GarminPersonalApiClient(t))
    assert auth.calls == [False]
