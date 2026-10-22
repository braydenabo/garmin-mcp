from typing import Any

import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordSetError

from garmin_connect_personal_mcp import tunnel_credentials as credentials


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[service, username] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[service, username]


@pytest.fixture
def native_store(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    # Never access the test runner's actual credential store.
    store = MemoryKeyring()
    monkeypatch.setattr(MemoryKeyring, "__module__", "keyring.backends.macOS")
    monkeypatch.setattr(credentials.keyring, "get_keyring", lambda: store)
    return store


def test_save_load_and_forget_are_scoped_to_profile(native_store: MemoryKeyring) -> None:
    credentials.save("one", "synthetic-one")
    credentials.save("two", "synthetic-two")
    assert credentials.load("one") == "synthetic-one"
    assert credentials.load("two") == "synthetic-two"
    credentials.forget("one")
    credentials.forget("one")  # forgetting an absent entry is harmless
    assert credentials.load("one") is None
    assert credentials.load("two") == "synthetic-two"
    assert set(native_store.values) == {(credentials.SERVICE, "two")}


def test_plaintext_or_other_non_native_backends_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryKeyring()
    monkeypatch.setattr(credentials.keyring, "get_keyring", lambda: store)
    with pytest.raises(credentials.CredentialError, match="No supported OS keychain"):
        credentials.save("example", "synthetic-key")
    assert not store.values


def test_keychain_errors_do_not_expose_key_material(
    native_store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any) -> None:
        raise PasswordSetError("sensitive underlying error")

    monkeypatch.setattr(native_store, "set_password", fail)
    with pytest.raises(credentials.CredentialError) as error:
        credentials.save("example", "synthetic-key")
    assert "sensitive" not in str(error.value)
