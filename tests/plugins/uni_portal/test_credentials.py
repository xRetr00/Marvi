"""Tests for the plugins/uni_portal/credentials.py OS-credential-store seam
(Marvi freedom spec §1.3). The real Windows Credential Manager is never
touched: ``_raw_write``/``_raw_read``/``_raw_delete`` (the only three
functions that call ``ctypes``/``Advapi32.dll``) are monkeypatched to an
in-memory fake dict, exercising the public API's contract independent of
platform.
"""

from __future__ import annotations

import pytest

from plugins.uni_portal import credentials as creds


@pytest.fixture(autouse=True)
def _fake_os_store(monkeypatch):
    """In-memory fake standing in for the real Windows Credential Manager."""
    store: dict[str, bytes] = {}

    def _write(target, blob):
        store[target] = blob
        return True

    def _read(target):
        return store.get(target)

    def _delete(target):
        existed = target in store
        store.pop(target, None)
        return existed

    monkeypatch.setattr(creds, "_raw_write", _write)
    monkeypatch.setattr(creds, "_raw_read", _read)
    monkeypatch.setattr(creds, "_raw_delete", _delete)
    return store


class TestStoreAndRead:
    def test_store_then_read_round_trips(self, _fake_os_store):
        assert creds.store_credentials("shereef", "hunter2") is True

        result = creds.read_credentials()

        assert result == {"username": "shereef", "password": "hunter2"}

    def test_read_with_nothing_stored_returns_none(self, _fake_os_store):
        assert creds.read_credentials() is None

    def test_has_credentials_reflects_store_state(self, _fake_os_store):
        assert creds.has_credentials() is False
        creds.store_credentials("shereef", "hunter2")
        assert creds.has_credentials() is True

    def test_empty_username_or_password_is_rejected(self, _fake_os_store):
        assert creds.store_credentials("", "hunter2") is False
        assert creds.store_credentials("shereef", "") is False
        assert _fake_os_store == {}

    def test_store_overwrites_previous_credentials(self, _fake_os_store):
        creds.store_credentials("shereef", "old-pass")
        creds.store_credentials("shereef", "new-pass")

        assert creds.read_credentials()["password"] == "new-pass"

    def test_only_one_target_name_used(self, _fake_os_store):
        """Duzce's portal supports one account per Marvi install (see
        module docstring) — confirm storage always uses the single fixed
        target name rather than one derived from the username."""
        creds.store_credentials("shereef", "hunter2")
        assert list(_fake_os_store.keys()) == [creds.TARGET_NAME]


class TestDelete:
    def test_delete_removes_stored_credentials(self, _fake_os_store):
        creds.store_credentials("shereef", "hunter2")
        assert creds.delete_credentials() is True
        assert creds.read_credentials() is None

    def test_delete_with_nothing_stored_is_still_success(self, _fake_os_store):
        assert creds.delete_credentials() is True


class TestMalformedStoredBlob:
    def test_non_json_blob_degrades_to_none(self, _fake_os_store, monkeypatch):
        monkeypatch.setattr(creds, "_raw_read", lambda target: b"not json at all")
        assert creds.read_credentials() is None

    def test_json_missing_fields_degrades_to_none(self, _fake_os_store, monkeypatch):
        import json

        monkeypatch.setattr(creds, "_raw_read", lambda target: json.dumps({"username": "shereef"}).encode())
        assert creds.read_credentials() is None

    def test_json_non_dict_degrades_to_none(self, _fake_os_store, monkeypatch):
        import json

        monkeypatch.setattr(creds, "_raw_read", lambda target: json.dumps(["a", "list"]).encode())
        assert creds.read_credentials() is None


class TestNeverRaises:
    def test_store_never_raises_when_backend_throws(self, monkeypatch):
        def _boom(target, blob):
            raise OSError("credential manager unavailable")

        monkeypatch.setattr(creds, "_raw_write", _boom)
        assert creds.store_credentials("shereef", "hunter2") is False

    def test_read_never_raises_when_backend_throws(self, monkeypatch):
        def _boom(target):
            raise OSError("credential manager unavailable")

        monkeypatch.setattr(creds, "_raw_read", _boom)
        assert creds.read_credentials() is None

    def test_delete_never_raises_when_backend_throws(self, monkeypatch):
        def _boom(target):
            raise OSError("credential manager unavailable")

        monkeypatch.setattr(creds, "_raw_delete", _boom)
        monkeypatch.setattr(creds, "_raw_read", lambda target: None)
        # A genuine backend exception is an honest failure (never raises to
        # the caller, but also never claims success it can't confirm).
        assert creds.delete_credentials() is False

    def test_delete_soft_succeeds_when_raw_delete_reports_false_but_nothing_remains(self, monkeypatch):
        """_raw_delete returning False (e.g. "target not found") is still a
        success as long as a follow-up read confirms nothing is stored."""
        monkeypatch.setattr(creds, "_raw_delete", lambda target: False)
        monkeypatch.setattr(creds, "_raw_read", lambda target: None)
        assert creds.delete_credentials() is True
