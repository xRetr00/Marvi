"""Tests for the ``NO_CHANGE`` wake-gate extension (Contract 1 of the
subconscious tick — see cron/subconscious.py).

``cron.scheduler._parse_wake_gate`` already understood the JSON
``{"wakeAgent": false}`` convention (see tests/cron/test_scheduler.py for
that coverage); this file covers only the new literal-``NO_CHANGE`` path
added alongside it, kept in a separate new file per the "tests/ — new test
files only" ownership boundary for this workstream.
"""

from cron.scheduler import _parse_wake_gate


class TestNoChangeWakeGate:
    def test_bare_no_change_skips_wake(self):
        assert _parse_wake_gate("NO_CHANGE") is False

    def test_no_change_with_trailing_whitespace_skips_wake(self):
        assert _parse_wake_gate("NO_CHANGE\n\n") is False
        assert _parse_wake_gate("  NO_CHANGE  ") is False

    def test_no_change_as_last_line_of_diagnostics_skips_wake(self):
        multi = "checked gmail: 0 new\nchecked calendar: 0 new\nNO_CHANGE"
        assert _parse_wake_gate(multi) is False

    def test_no_change_not_on_last_line_wakes_normally(self):
        # NO_CHANGE must be the LAST line to gate — otherwise it's just
        # incidental text inside a real diff and the agent should wake.
        multi = "NO_CHANGE\nbut actually here is a diff after all"
        assert _parse_wake_gate(multi) is True

    def test_diff_output_wakes_normally(self):
        diff = "gmail: 3 new messages\ncalendar: 1 new event"
        assert _parse_wake_gate(diff) is True

    def test_lowercase_no_change_does_not_match(self):
        # The contract is the exact literal token; case variants are treated
        # as ordinary diff text so a script author can't accidentally gate
        # silently on a near-miss like "no_change".
        assert _parse_wake_gate("no_change") is True

    def test_json_wake_gate_still_works_alongside_no_change(self):
        assert _parse_wake_gate('{"wakeAgent": false}') is False
        assert _parse_wake_gate('{"wakeAgent": true}') is True
