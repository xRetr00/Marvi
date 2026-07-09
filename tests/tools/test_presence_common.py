"""Tests for tools/presence/common.py -- the denylist filter and focus-app
heuristic shared by desktop_context, distill, and the flow gate.
"""

from tools.presence.common import (
    filter_denylisted_events,
    is_focus_app,
    is_terminal_app,
    is_vscode_app,
    matches_denylist,
    redact_if_denylisted,
)


class TestMatchesDenylist:
    def test_empty_denylist_never_matches(self):
        assert matches_denylist("anything at all", []) is False

    def test_case_insensitive_substring_match(self):
        assert matches_denylist("Private Banking - Chrome", ["banking"]) is True

    def test_no_match(self):
        assert matches_denylist("hermes-agent - Visual Studio Code", ["banking"]) is False

    def test_ignores_blank_needles(self):
        assert matches_denylist("some title", ["", "  "]) is False


class TestFilterDenylistedEvents:
    def test_empty_denylist_passes_through(self):
        events = [{"app": "chrome.exe", "title": "anything"}]
        assert filter_denylisted_events(events, []) == events

    def test_flat_events_filtered_by_app_or_title(self):
        events = [
            {"app": "chrome.exe", "title": "Private Banking Login"},
            {"app": "Code.exe", "title": "main.py - hermes-agent - Visual Studio Code"},
        ]
        result = filter_denylisted_events(events, ["banking"])
        assert len(result) == 1
        assert result[0]["app"] == "Code.exe"

    def test_nested_aw_event_shape(self):
        events = [
            {"data": {"app": "chrome.exe", "title": "my-bank.com - Chrome"}, "duration": 30},
            {"data": {"app": "Code.exe", "title": "main.py - Visual Studio Code"}, "duration": 120},
        ]
        result = filter_denylisted_events(events, ["my-bank"])
        assert len(result) == 1
        assert result[0]["data"]["app"] == "Code.exe"

    def test_no_matches_keeps_all_events(self):
        events = [{"app": "Code.exe", "title": "main.py"}, {"app": "chrome.exe", "title": "docs"}]
        assert filter_denylisted_events(events, ["banking"]) == events


class TestRedactIfDenylisted:
    def test_no_denylist_no_redaction(self):
        assert redact_if_denylisted("chrome.exe", "anything", []) is None

    def test_match_returns_reason(self):
        reason = redact_if_denylisted("chrome.exe", "Private Banking", ["banking"])
        assert reason is not None
        assert "denylist" in reason

    def test_no_match_returns_none(self):
        assert redact_if_denylisted("Code.exe", "main.py", ["banking"]) is None


class TestAppHeuristics:
    def test_is_focus_app_editor(self):
        assert is_focus_app("Code.exe") is True
        assert is_focus_app("pycharm64.exe") is True

    def test_is_focus_app_terminal(self):
        assert is_focus_app("Windows Terminal") is True
        assert is_focus_app("powershell.exe") is True

    def test_is_focus_app_browser_is_false(self):
        assert is_focus_app("chrome.exe") is False
        assert is_focus_app(None) is False

    def test_is_terminal_app(self):
        assert is_terminal_app("cmd.exe") is True
        assert is_terminal_app("Code.exe") is False

    def test_is_vscode_app(self):
        assert is_vscode_app("Code.exe") is True
        assert is_vscode_app("chrome.exe", "x - Visual Studio Code") is True
        assert is_vscode_app("chrome.exe", "Inbox - Gmail") is False
