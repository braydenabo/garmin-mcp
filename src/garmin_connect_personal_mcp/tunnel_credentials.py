"""Keep tunnel runtime keys in native OS credential stores, never plaintext files."""

import keyring
from keyring.backend import KeyringBackend
from keyring.backends.chainer import ChainerBackend
from keyring.errors import KeyringError

from garmin_connect_personal_mcp.config import APP_NAME

SERVICE = APP_NAME + "/chatgpt-tunnel"
NATIVE_BACKENDS = {
    "keyring.backends.macOS",
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
    "keyring.backends.kwallet",
}


class CredentialError(Exception):
    """The native credential store is unavailable or refused access."""


def _backend() -> KeyringBackend:
    selected = keyring.get_keyring()
    candidates = selected.backends if isinstance(selected, ChainerBackend) else [selected]
    for candidate in candidates:
        if type(candidate).__module__ in NATIVE_BACKENDS:
            return candidate
    raise CredentialError(
        "No supported OS keychain is available; use the hidden prompt or environment."
    )


def load(profile: str) -> str | None:
    try:
        return _backend().get_password(SERVICE, profile)
    except (KeyringError, RuntimeError, OSError) as exc:
        raise CredentialError(
            "Could not read the saved runtime key from your OS keychain."
        ) from exc


def save(profile: str, key: str) -> None:
    try:
        _backend().set_password(SERVICE, profile, key)
    except (KeyringError, RuntimeError, OSError) as exc:
        raise CredentialError("Could not save the runtime key in your OS keychain.") from exc


def forget(profile: str) -> None:
    try:
        backend = _backend()
        if backend.get_password(SERVICE, profile) is not None:
            backend.delete_password(SERVICE, profile)
    except (KeyringError, RuntimeError, OSError) as exc:
        raise CredentialError("Could not remove the runtime key from your OS keychain.") from exc
