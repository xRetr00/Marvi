"""Tests for the per-surface Composio delta fetchers (gmail.py / github.py).

The Composio SDK is fully mocked -- these tests never import the real
``composio`` package. Each fake client implements just the surface of
``ComposioClient`` the fetcher under test actually calls.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from cron.scripts.subconscious import github, gmail
from cron.scripts.subconscious.composio_client import ComposioAuthError
from cron.scripts.subconscious.snapshot_store import open_store


class FakeComposioClient:
    """Records calls and returns scripted responses per action slug."""

    def __init__(self, responses: Dict[str, Any]):
        self._responses = responses
        self.calls: List[tuple] = []

    def execute_action(self, action: str, params: Optional[Dict[str, Any]] = None, *, user_id: str = "default"):
        self.calls.append((action, params))
        resp = self._responses.get(action)
        if isinstance(resp, Exception):
            raise resp
        if callable(resp):
            return resp(params)
        return resp if resp is not None else {}


@pytest.fixture
def patch_gmail_client(monkeypatch):
    def _patch(responses: Dict[str, Any]) -> FakeComposioClient:
        client = FakeComposioClient(responses)
        monkeypatch.setattr(gmail, "get_client", lambda: client)
        return client

    return _patch


@pytest.fixture
def patch_github_client(monkeypatch):
    def _patch(responses: Dict[str, Any]) -> FakeComposioClient:
        client = FakeComposioClient(responses)
        monkeypatch.setattr(github, "get_client", lambda: client)
        return client

    return _patch


# ─── Gmail ──────────────────────────────────────────────────────────────


class TestGmailFetchDelta:
    def test_first_run_establishes_cursor_and_reports_nothing(self, patch_gmail_client):
        patch_gmail_client({gmail.ACTION_GET_PROFILE: {"historyId": "1000"}})
        store = open_store("gmail")

        result = gmail.fetch_delta(store)

        assert result is None
        assert store.cursor == {"history_id": "1000"}

    def test_no_new_messages_returns_none_and_advances_cursor(self, patch_gmail_client):
        store = open_store("gmail")
        store.set_cursor({"history_id": "1000"})
        client = patch_gmail_client(
            {gmail.ACTION_LIST_HISTORY: {"historyId": "1005", "history": []}}
        )

        result = gmail.fetch_delta(store)

        assert result is None
        assert store.cursor == {"history_id": "1005"}
        assert client.calls[0][0] == gmail.ACTION_LIST_HISTORY

    def test_new_important_message_is_summarized(self, patch_gmail_client):
        store = open_store("gmail")
        store.set_cursor({"history_id": "1000"})

        history_payload = {
            "historyId": "1010",
            "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
        }
        message_payload = {
            "labelIds": ["IMPORTANT", "INBOX"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "boss@example.com"},
                    {"name": "Subject", "value": "Q3 numbers"},
                ]
            },
        }
        patch_gmail_client(
            {
                gmail.ACTION_LIST_HISTORY: history_payload,
                gmail.ACTION_GET_MESSAGE: message_payload,
            }
        )

        result = gmail.fetch_delta(store)

        assert result is not None
        assert "Gmail: 1 new important/unread message(s)" in result
        assert "boss@example.com: Q3 numbers" in result
        assert store.cursor == {"history_id": "1010"}
        assert store.state["seen_message_ids"] == ["m1"]

    def test_unimportant_message_is_not_summarized_but_still_marked_seen(self, patch_gmail_client):
        store = open_store("gmail")
        store.set_cursor({"history_id": "1000"})

        history_payload = {
            "historyId": "1010",
            "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
        }
        message_payload = {"labelIds": ["CATEGORY_PROMOTIONS"], "payload": {"headers": []}}
        patch_gmail_client(
            {
                gmail.ACTION_LIST_HISTORY: history_payload,
                gmail.ACTION_GET_MESSAGE: message_payload,
            }
        )

        result = gmail.fetch_delta(store)

        assert result is None
        assert store.state["seen_message_ids"] == ["m1"]

    def test_already_seen_message_is_not_refetched(self, patch_gmail_client):
        store = open_store("gmail")
        store.set_cursor({"history_id": "1000"})
        store.set_state({"seen_message_ids": ["m1"]})

        history_payload = {
            "historyId": "1010",
            "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
        }
        client = patch_gmail_client({gmail.ACTION_LIST_HISTORY: history_payload})

        result = gmail.fetch_delta(store)

        assert result is None
        # GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID must never be called for an
        # already-seen id -- that would be exactly the wasted-API-call
        # pattern this sync is designed to avoid.
        assert all(call[0] != gmail.ACTION_GET_MESSAGE for call in client.calls)

    def test_auth_error_propagates_instead_of_being_swallowed(self, patch_gmail_client):
        patch_gmail_client({gmail.ACTION_GET_PROFILE: ComposioAuthError("bad key")})
        store = open_store("gmail")

        with pytest.raises(ComposioAuthError):
            gmail.fetch_delta(store)


# ─── GitHub ─────────────────────────────────────────────────────────────


class TestGithubFetchDelta:
    def test_first_run_establishes_cursor_and_reports_nothing(self, patch_github_client):
        patch_github_client({})
        store = open_store("github")

        result = github.fetch_delta(store)

        assert result is None
        assert "since" in store.cursor

    def test_no_notifications_returns_none(self, patch_github_client):
        store = open_store("github")
        store.set_cursor({"since": "2026-07-09T00:00:00+00:00", "etag": None})
        patch_github_client({github.ACTION_LIST_NOTIFICATIONS: {"data": []}})

        result = github.fetch_delta(store)

        assert result is None

    def test_new_notification_is_summarized(self, patch_github_client):
        store = open_store("github")
        store.set_cursor({"since": "2026-07-09T00:00:00+00:00", "etag": None})

        notifications = {
            "data": [
                {
                    "id": "n1",
                    "reason": "mention",
                    "subject": {"title": "Fix the bug", "type": "Issue"},
                    "repository": {"full_name": "xRetro00/Marvi"},
                }
            ],
            "etag": "W/\"abc\"",
        }
        patch_github_client({github.ACTION_LIST_NOTIFICATIONS: notifications})

        result = github.fetch_delta(store)

        assert result is not None
        assert "GitHub: 1 new notification(s)" in result
        assert "xRetro00/Marvi [Issue] Fix the bug" in result
        assert store.state["seen_ids"] == ["n1"]
        assert store.cursor["etag"] == "W/\"abc\""

    def test_already_seen_notification_is_not_resummarized(self, patch_github_client):
        store = open_store("github")
        store.set_cursor({"since": "2026-07-09T00:00:00+00:00", "etag": None})
        store.set_state({"seen_ids": ["n1"]})

        notifications = {
            "data": [
                {
                    "id": "n1",
                    "subject": {"title": "Fix the bug", "type": "Issue"},
                    "repository": {"full_name": "xRetro00/Marvi"},
                }
            ]
        }
        patch_github_client({github.ACTION_LIST_NOTIFICATIONS: notifications})

        result = github.fetch_delta(store)

        assert result is None

    def test_not_modified_short_circuits(self, patch_github_client):
        store = open_store("github")
        store.set_cursor({"since": "2026-07-09T00:00:00+00:00", "etag": "W/\"abc\""})
        client = patch_github_client({github.ACTION_LIST_NOTIFICATIONS: {"not_modified": True}})

        result = github.fetch_delta(store)

        assert result is None
        assert client.calls[0][1]["if_none_match"] == "W/\"abc\""

    def test_rate_limit_propagates(self, patch_github_client):
        from cron.scripts.subconscious.composio_client import ComposioRateLimited

        store = open_store("github")
        store.set_cursor({"since": "2026-07-09T00:00:00+00:00", "etag": None})
        patch_github_client({github.ACTION_LIST_NOTIFICATIONS: ComposioRateLimited()})

        with pytest.raises(ComposioRateLimited):
            github.fetch_delta(store)
