"""Tests for the Calendar and Slack Composio delta fetchers.

Sibling to ``test_subconscious_fetchers.py`` (same fake-client pattern, kept
in its own file so this workstream doesn't touch the gmail/github test
file). The Composio SDK is fully mocked -- these tests never import the
real ``composio`` package.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

import pytest

from cron.scripts.subconscious import calendar, slack
from cron.scripts.subconscious.composio_client import (
    ComposioAuthError,
    ComposioRateLimited,
    ComposioSyncTokenExpired,
)
from cron.scripts.subconscious.snapshot_store import open_store
from hermes_time import now as hermes_now


class FakeComposioClient:
    """Records calls and returns scripted responses per action slug.

    A response may be a plain value, an ``Exception`` instance (raised), or
    a callable ``(params) -> value`` for responses that depend on the call's
    params (e.g. calendar's baseline-vs-delta call using the same slug).
    """

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
def patch_calendar_client(monkeypatch):
    def _patch(responses: Dict[str, Any]) -> FakeComposioClient:
        client = FakeComposioClient(responses)
        monkeypatch.setattr(calendar, "get_client", lambda: client)
        return client

    return _patch


@pytest.fixture
def patch_slack_client(monkeypatch):
    def _patch(responses: Dict[str, Any]) -> FakeComposioClient:
        client = FakeComposioClient(responses)
        monkeypatch.setattr(slack, "get_client", lambda: client)
        return client

    return _patch


# ─── Calendar ───────────────────────────────────────────────────────────


class TestCalendarFetchDelta:
    def test_first_run_establishes_sync_token_and_reports_nothing(self, patch_calendar_client):
        patch_calendar_client(
            {calendar.ACTION_LIST_EVENTS: {"nextSyncToken": "tok1", "items": [{"id": "e0", "summary": "noise"}]}}
        )
        store = open_store("calendar")

        result = calendar.fetch_delta(store)

        assert result is None
        assert store.cursor == {"sync_token": "tok1"}

    def test_new_event_in_next_7_days_is_reported(self, patch_calendar_client):
        store = open_store("calendar")
        store.set_cursor({"sync_token": "tok1"})
        when = hermes_now() + timedelta(days=2)

        payload = {
            "nextSyncToken": "tok2",
            "items": [
                {
                    "id": "e1",
                    "summary": "Standup",
                    "status": "confirmed",
                    "start": {"dateTime": when.isoformat()},
                }
            ],
        }
        client = patch_calendar_client({calendar.ACTION_LIST_EVENTS: payload})

        result = calendar.fetch_delta(store)

        assert result is not None
        assert "Calendar: 1 change(s) in the next 7 days" in result
        assert "Standup" in result
        assert "(new)" in result
        assert store.cursor == {"sync_token": "tok2"}
        assert "e1" in store.state["known_events"]
        assert client.calls[0][1]["sync_token"] == "tok1"

    def test_event_outside_window_is_not_reported_but_remembered(self, patch_calendar_client):
        store = open_store("calendar")
        store.set_cursor({"sync_token": "tok1"})
        far_future = hermes_now() + timedelta(days=30)

        payload = {
            "nextSyncToken": "tok2",
            "items": [
                {
                    "id": "e1",
                    "summary": "Quarterly review",
                    "status": "confirmed",
                    "start": {"dateTime": far_future.isoformat()},
                }
            ],
        }
        patch_calendar_client({calendar.ACTION_LIST_EVENTS: payload})

        result = calendar.fetch_delta(store)

        assert result is None
        assert store.state["known_events"]["e1"]["when"] is None

    def test_cancelled_event_previously_known_is_reported(self, patch_calendar_client):
        store = open_store("calendar")
        store.set_cursor({"sync_token": "tok1"})
        store.set_state({"known_events": {"e1": {"title": "Standup", "when": "Wed Jul 15, 2:00 PM"}}})

        payload = {"nextSyncToken": "tok2", "items": [{"id": "e1", "status": "cancelled"}]}
        patch_calendar_client({calendar.ACTION_LIST_EVENTS: payload})

        result = calendar.fetch_delta(store)

        assert result is not None
        assert "Standup" in result
        assert "(cancelled)" in result
        assert "e1" not in store.state["known_events"]

    def test_cancelled_event_never_seen_and_no_start_is_not_reported(self, patch_calendar_client):
        # A cancelled event with no prior known state and no start info is
        # nothing the user was ever told about -- must not be surfaced.
        store = open_store("calendar")
        store.set_cursor({"sync_token": "tok1"})

        payload = {"nextSyncToken": "tok2", "items": [{"id": "e1", "status": "cancelled"}]}
        patch_calendar_client({calendar.ACTION_LIST_EVENTS: payload})

        result = calendar.fetch_delta(store)

        assert result is None

    def test_renamed_and_rescheduled_event_reports_what_changed(self, patch_calendar_client):
        store = open_store("calendar")
        store.set_cursor({"sync_token": "tok1"})
        store.set_state(
            {"known_events": {"e1": {"title": "Standup", "when": "Wed Jul 15, 2:00 PM"}}}
        )
        new_when = hermes_now() + timedelta(days=3)

        payload = {
            "nextSyncToken": "tok2",
            "items": [
                {
                    "id": "e1",
                    "summary": "Standup (moved)",
                    "status": "confirmed",
                    "start": {"dateTime": new_when.isoformat()},
                }
            ],
        }
        patch_calendar_client({calendar.ACTION_LIST_EVENTS: payload})

        result = calendar.fetch_delta(store)

        assert result is not None
        assert "Standup (moved)" in result
        assert "renamed" in result
        assert "rescheduled" in result

    def test_all_day_event_is_formatted_without_a_time(self, patch_calendar_client):
        store = open_store("calendar")
        store.set_cursor({"sync_token": "tok1"})
        when = hermes_now() + timedelta(days=1)

        payload = {
            "nextSyncToken": "tok2",
            "items": [
                {
                    "id": "e1",
                    "summary": "Company holiday",
                    "status": "confirmed",
                    "start": {"date": when.date().isoformat()},
                }
            ],
        }
        patch_calendar_client({calendar.ACTION_LIST_EVENTS: payload})

        result = calendar.fetch_delta(store)

        assert result is not None
        assert "(all day)" in result

    def test_sync_token_gone_triggers_rebaseline_without_dumping(self, patch_calendar_client):
        store = open_store("calendar")
        store.set_cursor({"sync_token": "stale-token"})

        def _respond(params):
            if params and params.get("sync_token"):
                raise ComposioSyncTokenExpired("410")
            # Re-baseline call (no sync_token param): pretend the calendar
            # has plenty of events -- none of this should be summarized.
            return {
                "nextSyncToken": "tok-fresh",
                "items": [{"id": f"e{i}", "summary": f"Event {i}"} for i in range(50)],
            }

        client = patch_calendar_client({calendar.ACTION_LIST_EVENTS: _respond})

        result = calendar.fetch_delta(store)

        assert result is None
        assert store.cursor == {"sync_token": "tok-fresh"}
        assert len(client.calls) == 2  # the failed delta call + the re-baseline call

    def test_no_events_in_delta_returns_none(self, patch_calendar_client):
        store = open_store("calendar")
        store.set_cursor({"sync_token": "tok1"})
        patch_calendar_client({calendar.ACTION_LIST_EVENTS: {"nextSyncToken": "tok2", "items": []}})

        result = calendar.fetch_delta(store)

        assert result is None
        assert store.cursor == {"sync_token": "tok2"}

    def test_auth_error_propagates_instead_of_being_swallowed(self, patch_calendar_client):
        store = open_store("calendar")
        store.set_cursor({"sync_token": "tok1"})
        patch_calendar_client({calendar.ACTION_LIST_EVENTS: ComposioAuthError("bad key")})

        with pytest.raises(ComposioAuthError):
            calendar.fetch_delta(store)


# ─── Slack ──────────────────────────────────────────────────────────────


class TestSlackFetchDelta:
    def test_first_run_establishes_cursor_reports_nothing_and_makes_no_call(self, patch_slack_client):
        client = patch_slack_client({})
        store = open_store("slack")

        result = slack.fetch_delta(store)

        assert result is None
        assert "last_ts" in store.cursor
        assert client.calls == []

    def test_no_new_items_returns_none_and_advances_cursor(self, patch_slack_client):
        store = open_store("slack")
        store.set_cursor({"last_ts": "1000.000000"})
        patch_slack_client({slack.ACTION_LIST_CONVERSATIONS: {"conversations": []}})

        result = slack.fetch_delta(store)

        assert result is None
        assert store.cursor["last_ts"] != "1000.000000"

    def test_new_dm_and_mention_are_summarized(self, patch_slack_client):
        store = open_store("slack")
        store.set_cursor({"last_ts": "1000.000000"})

        items = {
            "conversations": [
                {
                    "id": "D1",
                    "kind": "im",
                    "name": "alice",
                    "ts": "1001.0001",
                    "text": "hey can you look at this PR when you get a chance",
                },
                {
                    "id": "C1",
                    "kind": "channel",
                    "name": "carol",
                    "channel": "#eng",
                    "ts": "1002.0001",
                    "text": "@you thoughts on this?",
                },
            ]
        }
        patch_slack_client({slack.ACTION_LIST_CONVERSATIONS: items})

        result = slack.fetch_delta(store)

        assert result is not None
        assert "Slack: 1 new direct message(s), 1 thread mention(s)" in result
        assert "DM alice: hey can you look at this PR when you get a chance" in result
        assert "Mention #eng (carol): @you thoughts on this?" in result
        assert set(store.state["seen_ids"]) == {"D1:1001.0001", "C1:1002.0001"}

    def test_long_message_text_is_truncated_to_80_chars(self, patch_slack_client):
        store = open_store("slack")
        store.set_cursor({"last_ts": "1000.000000"})
        long_text = "x" * 200

        items = {"conversations": [{"id": "D1", "kind": "im", "name": "alice", "ts": "1001.0", "text": long_text}]}
        patch_slack_client({slack.ACTION_LIST_CONVERSATIONS: items})

        result = slack.fetch_delta(store)

        assert "x" * 80 in result
        assert "x" * 81 not in result
        assert "..." in result

    def test_items_beyond_cap_are_rolled_into_a_more_note(self, patch_slack_client):
        store = open_store("slack")
        store.set_cursor({"last_ts": "1000.000000"})

        conversations = [
            {"id": f"D{i}", "kind": "im", "name": f"user{i}", "ts": f"{1001 + i}.0", "text": "hi"}
            for i in range(25)
        ]
        patch_slack_client({slack.ACTION_LIST_CONVERSATIONS: {"conversations": conversations}})

        result = slack.fetch_delta(store)

        assert "+5 more" in result
        assert "Slack: 25 new direct message(s), 0 thread mention(s)" in result
        # Exactly MAX_ITEMS_PER_RUN individual lines plus the "+N more" line.
        summary_lines = [ln for ln in result.splitlines() if ln.strip().startswith("-")]
        assert len(summary_lines) == slack.MAX_ITEMS_PER_RUN + 1

    def test_already_seen_item_is_not_resummarized(self, patch_slack_client):
        store = open_store("slack")
        store.set_cursor({"last_ts": "1000.000000"})
        store.set_state({"seen_ids": ["D1:1001.0"]})

        items = {
            "conversations": [
                {"id": "D1", "kind": "im", "name": "alice", "ts": "1001.0", "text": "already seen"},
                {"id": "D2", "kind": "im", "name": "bob", "ts": "1002.0", "text": "brand new"},
            ]
        }
        patch_slack_client({slack.ACTION_LIST_CONVERSATIONS: items})

        result = slack.fetch_delta(store)

        assert result is not None
        assert "Slack: 1 new direct message(s), 0 thread mention(s)" in result
        assert "bob" in result
        assert "already seen" not in result

    def test_rate_limit_propagates_instead_of_being_swallowed(self, patch_slack_client):
        store = open_store("slack")
        store.set_cursor({"last_ts": "1000.000000"})
        patch_slack_client({slack.ACTION_LIST_CONVERSATIONS: ComposioRateLimited()})

        with pytest.raises(ComposioRateLimited):
            slack.fetch_delta(store)
