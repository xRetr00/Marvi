"""Tests for tools/presence/goblin.py's check_stuck heuristic.

Fixtures are AW window-event streams, newest first, shaped like
AWClient.get_events()'s output: {"timestamp": iso, "duration": seconds,
"data": {"app": ..., "title": ...}}.
"""

from tools.presence.goblin import (
    STUCK_MIN_DURATION_SECONDS,
    check_stuck,
)


def _event(app: str, title: str, duration: float, ts: str = "2026-07-09T10:00:00Z") -> dict:
    return {"timestamp": ts, "duration": duration, "data": {"app": app, "title": title}}


class TestCheckStuckNoFalsePositives:
    def test_normal_long_focused_work_session_is_not_stuck(self):
        """A long, uninterrupted, error-free coding session must NOT trigger --
        duration alone is not enough; one of the two corroborating signals is
        required (design spec: ">45 min AND (error keywords OR rapid search
        switching)")."""
        title = "aw_client.py - hermes-agent - Visual Studio Code"
        events = [_event("Code.exe", title, 600) for _ in range(6)]  # 60 minutes total
        assert check_stuck(events) is None

    def test_short_session_with_error_keyword_is_not_stuck(self):
        """Error-looking title but under the 45-minute threshold: not stuck yet."""
        events = [_event("Code.exe", "main.py - TypeError: undefined - Visual Studio Code", 300)]
        assert check_stuck(events) is None

    def test_normal_app_switching_without_search_is_not_stuck(self):
        """Switching between a few ordinary apps (not search/SO tabs) plus a
        long current session must not be flagged."""
        events = [_event("Code.exe", "main.py - proj - Visual Studio Code", 2800)]
        events += [
            _event("Slack.exe", "general - Slack", 60),
            _event("Outlook.exe", "Inbox - Outlook", 120),
        ]
        assert check_stuck(events) is None

    def test_empty_events(self):
        assert check_stuck([]) is None

    def test_missing_title_is_ignored(self):
        assert check_stuck([_event("Code.exe", "", 3000)]) is None

    def test_malformed_event_does_not_raise(self):
        assert check_stuck([{"garbage": True}]) is None


class TestCheckStuckDetectsRealSignals:
    def test_error_keyword_in_current_title_triggers(self):
        events = [
            _event(
                "Code.exe",
                "main.py - TypeError: cannot read property - Visual Studio Code",
                STUCK_MIN_DURATION_SECONDS + 60,
            )
        ]
        finding = check_stuck(events)
        assert finding is not None
        assert finding["stuck"] is True
        assert finding["signal"] == "error_keyword"
        assert finding["duration_seconds"] >= STUCK_MIN_DURATION_SECONDS

    def test_error_keyword_seen_in_lookback_triggers(self):
        # Current window itself is clean, but the events leading up to it
        # (still within the same settled window) show an error-looking title.
        title = "main.py - proj - Visual Studio Code"
        events = [_event("Code.exe", title, STUCK_MIN_DURATION_SECONDS + 120)]
        finding = check_stuck(events)
        # Same-title stretch has no error keyword and no search switching --
        # not stuck (duration alone is insufficient, matches the no-false-
        # positive contract above).
        assert finding is None

    def test_rapid_search_tab_switching_triggers(self):
        title = "main.py - proj - Visual Studio Code"
        events = [_event("Code.exe", title, STUCK_MIN_DURATION_SECONDS + 60)]
        events += [
            _event("chrome.exe", "python list comprehension trick - Stack Overflow - Google Chrome", 20),
            _event("chrome.exe", "python list comprehension - Google Search", 15),
            _event("chrome.exe", "another list comprehension answer - Stack Overflow - Google Chrome", 25),
        ]
        finding = check_stuck(events)
        assert finding is not None
        assert finding["signal"] == "rapid_search_switching"

    def test_two_search_switches_not_enough(self):
        """Below the rapid-switch threshold (>=3): must not trigger."""
        title = "main.py - proj - Visual Studio Code"
        events = [_event("Code.exe", title, STUCK_MIN_DURATION_SECONDS + 60)]
        events += [
            _event("chrome.exe", "fix - Stack Overflow - Google Chrome", 20),
            _event("chrome.exe", "fix - Google Search", 15),
        ]
        assert check_stuck(events) is None
