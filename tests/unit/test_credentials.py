"""Tests for OS-keychain credential storage.

`keyring` is monkeypatched with an in-memory store so the round-trip is
exercised without touching the real OS keychain.
"""

from __future__ import annotations

from collections.abc import Iterator

import keyring.errors
import pytest

from src.credentials import (
    Credential,
    delete_credential,
    get_credential,
    set_credential,
)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[tuple[str, str], str]]:
    store: dict[tuple[str, str], str] = {}

    def _set(service: str, key: str, value: str) -> None:
        store[(service, key)] = value

    def _get(service: str, key: str) -> str | None:
        return store.get((service, key))

    def _del(service: str, key: str) -> None:
        if (service, key) in store:
            del store[(service, key)]
        else:
            raise keyring.errors.PasswordDeleteError("not found")

    monkeypatch.setattr("keyring.set_password", _set)
    monkeypatch.setattr("keyring.get_password", _get)
    monkeypatch.setattr("keyring.delete_password", _del)
    yield store


def test_set_and_get_roundtrip(fake_keyring: dict[tuple[str, str], str]) -> None:
    set_credential("betano", "user1", "pass1")
    assert get_credential("betano") == Credential("betano", "user1", "pass1")


def test_get_missing_returns_none(fake_keyring: dict[tuple[str, str], str]) -> None:
    assert get_credential("betsson") is None


def test_partial_credential_returns_none(fake_keyring: dict[tuple[str, str], str]) -> None:
    """Username present but password missing → treated as unset, not a half-credential."""
    fake_keyring[("arby", "bplay:username")] = "u"
    assert get_credential("bplay") is None


def test_password_excluded_from_repr() -> None:
    c = Credential("betano", "myuser", "s3cret-token")
    assert "s3cret-token" not in repr(c)
    assert "myuser" in repr(c)


def test_delete_removes(fake_keyring: dict[tuple[str, str], str]) -> None:
    set_credential("bplay", "u", "p")
    delete_credential("bplay")
    assert get_credential("bplay") is None


def test_delete_absent_is_noop(fake_keyring: dict[tuple[str, str], str]) -> None:
    delete_credential("betwarrior")  # must not raise even when nothing is stored
