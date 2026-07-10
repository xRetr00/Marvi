"""Tests for tools/presence/rhythm.py -- the per-weekday rhythm model.

compute_rhythm is pure (synthetic AW afk events in, dict out), so most
coverage needs no mocking at all. update_rhythm/get_rhythm use the per-test
isolated HERMES_HOME from the autouse ``_hermetic_environment`` fixture, and
the ActivityWatch client is faked at its source module (rhythm imports it
lazily inside update_rhythm, so patching the module attribute takes effect).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import tools.presence.aw_client as aw_client_mod
from tools.presence.rhythm import (
    ACTIVE_HOURS_MARGIN_MINUTES,
    LOOKBACK_DAYS,
    MIN_DAYS_WITH_ACTIVITY,
    RHYTHM_SCHEMA_VERSION,
    compute_rhythm,
    get_rhythm,
    is_outside_active_hours,
    rhythm_summary_line,
    update_rhythm,
    _rhythm_path,
)


def _evt(day: datetime, hour: int, minute: int = 0, *, hours: float = 1.0,
         status: str = "not-afk") -> dict:
    """Build one synthetic AW afk-bucket event starting on ``day`` at
    hour:minute lasting ``hours``."""
    ts = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return {
        "timestamp": ts.isoformat(),
        "duration": hours * 3600.0,
        "data": {"status": status},
    }


def _monday_on_or_before(dt: datetime) -> datetime:
    return dt - timedelta(days=dt.weekday())


# A fixed, recent-ish anchor Monday. compute_rhythm's lookback is relative
# to the newest event, so absolute recency doesn't matter for the pure tests.
_ANCHOR = _monday_on_or_before(datetime(2026, 7, 6, 12, 0))  # Monday 2026-07-06


class TestComputeRhythmBasics:
    def test_empty_events_returns_none(self):
        assert compute_rhythm([]) is None
        assert compute_rhythm(None) is None

    def test_insufficient_days_returns_none(self):
        # Only 2 distinct days with activity -- below MIN_DAYS_WITH_ACTIVITY.
        assert MIN_DAYS_WITH_ACTIVITY == 3
        events = [
            _evt(_ANCHOR, 9, hours=8),
            _evt(_ANCHOR + timedelta(days=1), 9, hours=8),
        ]
        assert compute_rhythm(events) is None

    def test_afk_events_do_not_count_as_activity(self):
        # 3 days of pure-afk events -> no activity at all -> None.
        events = [
            _evt(_ANCHOR + timedelta(days=i), 9, hours=8, status="afk")
            for i in range(3)
        ]
        assert compute_rhythm(events) is None

    def test_three_active_days_produce_per_weekday_entries(self):
        # Mon/Tue/Wed each 09:00-17:00.
        events = [
            _evt(_ANCHOR + timedelta(days=i), 9, hours=8) for i in range(3)
        ]
        rhythm = compute_rhythm(events)
        assert rhythm is not None
        assert rhythm["days_analyzed"] == 3
        weekdays = rhythm["weekdays"]
        assert set(weekdays) == {"0", "1", "2"}  # Mon, Tue, Wed
        for entry in weekdays.values():
            assert entry["active_start"] == "09:00"
            assert entry["active_end"] == "17:00"
            assert entry["deep_work_windows"] == [["09:00", "17:00"]]

    def test_zero_duration_and_malformed_events_are_ignored(self):
        good = [_evt(_ANCHOR + timedelta(days=i), 9, hours=8) for i in range(3)]
        junk = [
            {"timestamp": "not a timestamp", "duration": 3600, "data": {"status": "not-afk"}},
            {"timestamp": _ANCHOR.isoformat(), "duration": 0, "data": {"status": "not-afk"}},
            {"timestamp": _ANCHOR.isoformat(), "duration": "??", "data": {"status": "not-afk"}},
            "not even a dict",
            {},
        ]
        rhythm = compute_rhythm(good + junk)
        assert rhythm is not None
        assert rhythm["days_analyzed"] == 3


class TestComputeRhythmMedians:
    def test_median_of_three_same_weekday_days(self):
        # Three Mondays 14 days apart edge-to-edge: starts 10:00 / 09:00 /
        # 08:00, all 8h long. The oldest Monday's 10:00 start is still
        # >= (newest event - 14 days), so all three survive the lookback.
        m1, m2, m3 = _ANCHOR - timedelta(days=14), _ANCHOR - timedelta(days=7), _ANCHOR
        events = [
            _evt(m1, 10, hours=8),  # 10:00-18:00
            _evt(m2, 9, hours=8),   # 09:00-17:00
            _evt(m3, 8, hours=8),   # 08:00-16:00
        ]
        rhythm = compute_rhythm(events)
        assert rhythm is not None
        monday = rhythm["weekdays"]["0"]
        assert monday["active_start"] == "09:00"  # median of 08/09/10
        assert monday["active_end"] == "17:00"    # median of 16/17/18

    def test_median_interpolates_between_two_days(self):
        # Two Mondays (starts 08:00 and 10:00) + filler days for the
        # 3-day minimum. Median of [480, 600] minutes = 540 -> "09:00".
        events = [
            _evt(_ANCHOR - timedelta(days=7), 8, hours=8),   # Mon 08:00-16:00
            _evt(_ANCHOR, 10, hours=8),                       # Mon 10:00-18:00
            _evt(_ANCHOR + timedelta(days=1), 9, hours=4),    # Tue (filler)
        ]
        rhythm = compute_rhythm(events)
        monday = rhythm["weekdays"]["0"]
        assert monday["active_start"] == "09:00"
        assert monday["active_end"] == "17:00"

    def test_first_and_last_event_of_day_define_start_end(self):
        # Multiple events per day: start = first, end = last event's end.
        day = _ANCHOR
        events = [
            _evt(day, 8, 30, hours=1),    # 08:30-09:30
            _evt(day, 12, hours=2),       # 12:00-14:00
            _evt(day, 20, hours=1.5),     # 20:00-21:30
            _evt(day + timedelta(days=1), 9, hours=8),
            _evt(day + timedelta(days=2), 9, hours=8),
        ]
        rhythm = compute_rhythm(events)
        monday = rhythm["weekdays"]["0"]
        assert monday["active_start"] == "08:30"
        assert monday["active_end"] == "21:30"


class TestComputeRhythmDeepWork:
    def test_short_stretches_are_filtered_and_longest_two_kept(self):
        # One observed Monday with four stretches: 3h, 2h, 1h, 0.5h.
        # Expect the two longest (3h, 2h) in chronological order; the 30min
        # stretch is under the 60-minute floor and the 1h stretch loses the
        # top-2 cut.
        day = _ANCHOR
        events = [
            _evt(day, 8, hours=3),       # 08:00-11:00  (3h)
            _evt(day, 12, hours=2),      # 12:00-14:00  (2h)
            _evt(day, 15, hours=1),      # 15:00-16:00  (1h)
            _evt(day, 17, hours=0.5),    # 17:00-17:30  (30m, filtered)
            _evt(day + timedelta(days=1), 9, hours=8),
            _evt(day + timedelta(days=2), 9, hours=8),
        ]
        rhythm = compute_rhythm(events)
        monday = rhythm["weekdays"]["0"]
        assert monday["deep_work_windows"] == [["08:00", "11:00"], ["12:00", "14:00"]]

    def test_typical_windows_require_majority_of_observed_days(self):
        # Three Mondays: 10-18, 09-17, 08-16 (majority threshold = 2 days).
        # Only 09:00-17:00 is active on >= 2 Mondays; the 08-09 and 17-18
        # fringes each belong to a single day and drop out.
        m1, m2, m3 = _ANCHOR - timedelta(days=14), _ANCHOR - timedelta(days=7), _ANCHOR
        events = [
            _evt(m1, 10, hours=8),
            _evt(m2, 9, hours=8),
            _evt(m3, 8, hours=8),
        ]
        rhythm = compute_rhythm(events)
        monday = rhythm["weekdays"]["0"]
        assert monday["deep_work_windows"] == [["09:00", "17:00"]]

    def test_events_older_than_lookback_are_ignored(self):
        # A day 20 days before the newest event must not count toward
        # days_analyzed or the medians.
        old = _ANCHOR - timedelta(days=20)
        events = [
            _evt(old, 3, hours=2),  # would skew Monday start to 03:00
            _evt(_ANCHOR, 9, hours=8),
            _evt(_ANCHOR + timedelta(days=1), 9, hours=8),
            _evt(_ANCHOR + timedelta(days=2), 9, hours=8),
        ]
        rhythm = compute_rhythm(events)
        assert rhythm["days_analyzed"] == 3
        assert rhythm["weekdays"]["0"]["active_start"] == "09:00"


# ---------------------------------------------------------------------------
# Persistence: update_rhythm / get_rhythm
# ---------------------------------------------------------------------------


class FakeAWClient:
    def __init__(self, *, available=True, bucket="aw-watcher-afk_host", events=None):
        self.available = available
        self.bucket = bucket
        self.events = events or []
        self.get_events_calls = []

    def is_available(self, force=False):
        return self.available

    def find_bucket_id(self, prefix):
        if self.bucket and self.bucket.startswith(prefix):
            return self.bucket
        return None

    def get_events(self, bucket_id, *, start=None, end=None, limit=100):
        self.get_events_calls.append({"bucket_id": bucket_id, "start": start, "limit": limit})
        return list(self.events)


def _recent_events(days=3, hour=9, hours=8):
    now = datetime.now()
    return [
        _evt(now - timedelta(days=i + 1), hour, hours=hours) for i in range(days)
    ]


class TestUpdateAndGetRhythm:
    def test_update_writes_file_and_get_reads_it_back(self, monkeypatch):
        fake = FakeAWClient(events=_recent_events(days=4))
        monkeypatch.setattr(aw_client_mod, "aw_client", fake)

        assert update_rhythm() is True
        path = _rhythm_path()
        assert path.exists()
        # No leftover tempfiles from the atomic-write path.
        leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".rhythm_")]
        assert leftovers == []

        rhythm = get_rhythm()
        assert rhythm is not None
        assert rhythm["schema_version"] == RHYTHM_SCHEMA_VERSION
        assert rhythm["days_analyzed"] == 4
        assert rhythm["weekdays"]  # at least one weekday entry
        for entry in rhythm["weekdays"].values():
            assert entry["active_start"] == "09:00"
            assert entry["active_end"] == "17:00"

    def test_aw_unavailable_keeps_previous_file(self, monkeypatch):
        fake = FakeAWClient(events=_recent_events(days=4))
        monkeypatch.setattr(aw_client_mod, "aw_client", fake)
        assert update_rhythm() is True
        before = _rhythm_path().read_text(encoding="utf-8")

        monkeypatch.setattr(aw_client_mod, "aw_client", FakeAWClient(available=False))
        assert update_rhythm() is False
        assert _rhythm_path().read_text(encoding="utf-8") == before
        assert get_rhythm() is not None

    def test_missing_afk_bucket_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(aw_client_mod, "aw_client", FakeAWClient(bucket=None))
        assert update_rhythm() is False
        assert get_rhythm() is None

    def test_insufficient_data_writes_nothing(self, monkeypatch):
        fake = FakeAWClient(events=_recent_events(days=2))  # < 3 days
        monkeypatch.setattr(aw_client_mod, "aw_client", fake)
        assert update_rhythm() is False
        assert not _rhythm_path().exists()
        assert get_rhythm() is None

    def test_get_rhythm_none_when_absent(self):
        assert get_rhythm() is None

    def test_get_rhythm_none_when_corrupt(self):
        path = _rhythm_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{definitely not json", encoding="utf-8")
        assert get_rhythm() is None

    def test_get_rhythm_none_when_stale(self):
        path = _rhythm_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stale = datetime.now() - timedelta(days=LOOKBACK_DAYS + 1)
        path.write_text(json.dumps({
            "schema_version": RHYTHM_SCHEMA_VERSION,
            "generated_at": stale.isoformat(),
            "days_analyzed": 5,
            "weekdays": {"0": {"active_start": "09:00", "active_end": "17:00",
                               "deep_work_windows": []}},
        }), encoding="utf-8")
        assert get_rhythm() is None

    def test_get_rhythm_none_on_schema_version_mismatch(self):
        path = _rhythm_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": RHYTHM_SCHEMA_VERSION + 1,
            "generated_at": datetime.now().isoformat(),
            "weekdays": {},
        }), encoding="utf-8")
        assert get_rhythm() is None


# ---------------------------------------------------------------------------
# is_outside_active_hours / rhythm_summary_line
# ---------------------------------------------------------------------------


def _write_rhythm(weekdays: dict) -> None:
    path = _rhythm_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": RHYTHM_SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(),
        "days_analyzed": 5,
        "weekdays": weekdays,
    }), encoding="utf-8")


class TestIsOutsideActiveHours:
    # Wednesday 2026-07-08; weekday key "2".
    WED = datetime(2026, 7, 8)

    def _write_wed(self, start="09:00", end="17:00"):
        _write_rhythm({"2": {"active_start": start, "active_end": end,
                             "deep_work_windows": []}})

    def test_no_rhythm_file_is_never_outside(self):
        assert is_outside_active_hours(self.WED.replace(hour=3)) is False

    def test_clearly_before_active_start_is_outside(self):
        self._write_wed()
        assert is_outside_active_hours(self.WED.replace(hour=3)) is True

    def test_clearly_after_active_end_is_outside(self):
        self._write_wed()
        assert is_outside_active_hours(self.WED.replace(hour=23)) is True

    def test_inside_active_hours_is_not_outside(self):
        self._write_wed()
        assert is_outside_active_hours(self.WED.replace(hour=12)) is False

    def test_margin_keeps_near_boundary_times_inside(self):
        self._write_wed(start="09:00", end="17:00")
        assert ACTIVE_HOURS_MARGIN_MINUTES == 30
        # 08:45 is within the 30-min pre-start margin -> not outside.
        assert is_outside_active_hours(self.WED.replace(hour=8, minute=45)) is False
        # 08:00 is beyond the margin -> outside.
        assert is_outside_active_hours(self.WED.replace(hour=8, minute=0)) is True
        # 17:20 is within the post-end margin -> not outside.
        assert is_outside_active_hours(self.WED.replace(hour=17, minute=20)) is False

    def test_no_data_for_todays_weekday_is_not_outside(self):
        # Rhythm exists but only covers Monday; a Wednesday query can't be
        # classified -> conservative False (gate behaves as before).
        _write_rhythm({"0": {"active_start": "09:00", "active_end": "17:00",
                             "deep_work_windows": []}})
        assert is_outside_active_hours(self.WED.replace(hour=3)) is False

    def test_garbage_times_are_not_outside(self):
        self._write_wed(start="not-a-time", end="17:00")
        assert is_outside_active_hours(self.WED.replace(hour=3)) is False


class TestRhythmSummaryLine:
    WED = datetime(2026, 7, 8)

    def test_none_without_rhythm(self):
        assert rhythm_summary_line(self.WED) is None

    def test_summary_includes_active_window_and_deep_work(self):
        _write_rhythm({"2": {
            "active_start": "08:45", "active_end": "23:10",
            "deep_work_windows": [["09:00", "12:30"], ["14:00", "17:00"]],
        }})
        line = rhythm_summary_line(self.WED)
        assert line is not None
        assert "\n" not in line  # one line, ready for the digest
        assert "Wed" in line
        assert "08:45-23:10" in line
        assert "09:00-12:30" in line
        assert "14:00-17:00" in line

    def test_none_when_today_has_no_entry(self):
        _write_rhythm({"0": {"active_start": "09:00", "active_end": "17:00",
                             "deep_work_windows": []}})
        assert rhythm_summary_line(self.WED) is None


# ---------------------------------------------------------------------------
# Distill wiring
# ---------------------------------------------------------------------------


class FakeDistillAWClient:
    """Just enough AW surface for distill.build_digest: one window bucket."""

    def __init__(self, window_events):
        self.window_events = window_events

    def is_available(self, force=False):
        return True

    def find_bucket_id(self, prefix):
        return "aw-watcher-window_host" if prefix == "aw-watcher-window" else None

    def get_events(self, bucket_id, *, start=None, end=None, limit=100):
        return list(self.window_events)


class TestDistillWiring:
    def test_digest_appends_rhythm_summary_when_available(self, monkeypatch):
        from datetime import datetime as dt

        from tools.presence.distill import build_digest

        now = dt.now()
        _write_rhythm({str(now.weekday()): {
            "active_start": "08:45", "active_end": "23:10",
            "deep_work_windows": [["09:00", "12:30"]],
        }})
        events = [{
            "timestamp": now.isoformat(),
            "duration": 3600.0,
            "data": {"app": "Code.exe", "title": "hermes-agent"},
        }]
        monkeypatch.setattr(aw_client_mod, "aw_client", FakeDistillAWClient(events))

        digest = build_digest(since_iso=(now - timedelta(hours=2)).isoformat())
        assert "App usage" in digest
        assert "Typical rhythm" in digest
        assert "08:45-23:10" in digest

    def test_digest_unchanged_when_no_rhythm(self, monkeypatch):
        from datetime import datetime as dt

        from tools.presence.distill import build_digest

        now = dt.now()
        events = [{
            "timestamp": now.isoformat(),
            "duration": 3600.0,
            "data": {"app": "Code.exe", "title": "hermes-agent"},
        }]
        monkeypatch.setattr(aw_client_mod, "aw_client", FakeDistillAWClient(events))

        digest = build_digest(since_iso=(now - timedelta(hours=2)).isoformat())
        assert "App usage" in digest
        assert "Typical rhythm" not in digest

    def test_print_digest_for_cron_survives_rhythm_failure(self, monkeypatch, capsys):
        import tools.presence.rhythm as rhythm_module
        from tools.presence.distill import print_digest_for_cron

        def _boom():
            raise RuntimeError("rhythm exploded")

        monkeypatch.setattr(rhythm_module, "update_rhythm", _boom)
        monkeypatch.setattr(
            aw_client_mod, "aw_client",
            FakeAWClient(available=False),
        )
        print_digest_for_cron()  # must not raise
        assert capsys.readouterr().out == ""

    def test_print_digest_for_cron_calls_update_rhythm(self, monkeypatch):
        import tools.presence.rhythm as rhythm_module
        from tools.presence.distill import print_digest_for_cron

        calls = []
        monkeypatch.setattr(rhythm_module, "update_rhythm", lambda: calls.append(1) or True)
        monkeypatch.setattr(
            aw_client_mod, "aw_client",
            FakeAWClient(available=False),
        )
        print_digest_for_cron()
        assert calls == [1]
