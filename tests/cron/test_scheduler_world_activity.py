"""World-activity noise filtering + per-source rotation fairness.

Regression coverage for the activity-feed flood: plugins/smart_room's
state_store.append_transition() mirrors EVERY room transition into
cron.scheduler.record_subconscious_activity(source="world", ...)
unfiltered, which — combined with a globally-FIFO 500-line rotation cap —
let presence flapping and light/mode noise evict every reflection/goblin/
distiller entry from the shared activity.jsonl.

record_subconscious_activity(source="world", ...) is the append site this
module owns (plugins/smart_room itself is out of scope here), so these
tests drive it directly the same way append_transition does: source="world",
diff=json.dumps(event).
"""

import json

import pytest

import cron.scheduler as scheduler
from hermes_constants import get_hermes_home


def _activity_lines():
    path = get_hermes_home() / "subconscious" / "activity.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _world(event: dict, **kwargs) -> None:
    scheduler.record_subconscious_activity(
        source="world",
        outcome="diff_silent",
        summary=str(event.get("summary") or event.get("type") or "Room changed"),
        diff=json.dumps(event, ensure_ascii=False),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _reset_world_activity_state():
    """The meaningful/debounce filter tracks vacancy + last-logged-at as
    module-level state (mirrors gateway/world_trigger.py's own in-memory
    trade-off) — reset it around every test so cases can't leak into each
    other."""
    scheduler._world_vacant_since = None
    scheduler._world_last_logged_at = {}
    yield
    scheduler._world_vacant_since = None
    scheduler._world_last_logged_at = {}


class TestWorldActivityMeaningfulPassthrough:
    def test_owner_arrival_after_long_absence_is_logged(self):
        _world({"type": "presence_cleared", "at": "2026-07-20T10:00:00+00:00"})
        _world({
            "type": "room_entry",
            "at": "2026-07-20T10:35:00+00:00",  # 35 min later
            "classification": "owner",
            "summary": "Owner entered the room",
        })

        lines = _activity_lines()
        assert len(lines) == 1
        assert lines[0]["source"] == "world"

    def test_guest_arrival_after_long_absence_is_logged(self):
        _world({"type": "presence_cleared", "at": "2026-07-20T10:00:00+00:00"})
        _world({
            "type": "room_entry",
            "at": "2026-07-20T10:31:00+00:00",  # 31 min later, >= 30 min bar
            "classification": "guest",
            "summary": "Guest entered the room",
        })

        assert len(_activity_lines()) == 1

    def test_arrival_after_short_absence_is_not_logged(self):
        _world({"type": "presence_cleared", "at": "2026-07-20T10:00:00+00:00"})
        _world({
            "type": "room_entry",
            "at": "2026-07-20T10:05:00+00:00",  # only 5 min later
            "classification": "guest",
            "summary": "Guest entered the room",
        })

        assert _activity_lines() == []

    def test_arrival_with_no_preceding_vacancy_observed_is_not_logged(self):
        # No presence_cleared was ever seen this process lifetime -- can't
        # prove a real absence, so treat as not meaningful (matches
        # gateway/world_trigger.py's own "no vacancy_seconds -> not
        # wake-worthy" behavior for presence_detected).
        _world({
            "type": "room_entry",
            "at": "2026-07-20T10:00:00+00:00",
            "classification": "owner",
            "summary": "Owner entered the room",
        })

        assert _activity_lines() == []

    def test_phone_arrive_home_is_logged(self):
        _world({"type": "phone_location_changed", "transition": "arrive", "zone": "home", "at": "2026-07-20T10:00:00+00:00"})

        assert len(_activity_lines()) == 1

    def test_phone_leave_home_is_logged(self):
        _world({"type": "phone_location_changed", "transition": "leave", "zone": "home", "at": "2026-07-20T10:00:00+00:00"})

        assert len(_activity_lines()) == 1

    def test_phone_transition_at_non_home_zone_is_not_logged(self):
        _world({"type": "phone_location_changed", "transition": "arrive", "zone": "university", "at": "2026-07-20T10:00:00+00:00"})

        assert _activity_lines() == []

    def test_device_offline_is_logged(self):
        _world({"type": "device_offline", "device": "esp32", "at": "2026-07-20T10:00:00+00:00"})

        assert len(_activity_lines()) == 1


class TestWorldActivityNeverLogged:
    """light/mode changes must never get a world activity entry -- they
    still flow to the tick diff via cron/scripts/subconscious/smart_room.py
    (a separate channel, events.jsonl, untouched by this filter)."""

    def test_light_changed_never_logged(self):
        _world({"type": "light_changed", "at": "2026-07-20T10:00:00+00:00", "summary": "light changed"})

        assert _activity_lines() == []

    def test_mode_changed_never_logged(self):
        _world({"type": "mode_changed", "at": "2026-07-20T10:00:00+00:00", "mode": "focus"})

        assert _activity_lines() == []

    def test_presence_detected_never_logged_directly(self):
        # The raw bus event -- room_entry is the classified signal we log,
        # not this one, even when a long vacancy preceded it.
        _world({"type": "presence_cleared", "at": "2026-07-20T10:00:00+00:00"})
        _world({"type": "presence_detected", "at": "2026-07-20T11:00:00+00:00", "vacancy_seconds": 3600})

        assert _activity_lines() == []

    def test_sleep_cancelled_never_logged(self):
        _world({"type": "sleep_cancelled", "at": "2026-07-20T10:00:00+00:00", "reason": "work_return"})

        assert _activity_lines() == []

    def test_geofence_arrive_home_never_logged_directly(self):
        # geofence_arrive_home/leave_home are companion events app.py emits
        # alongside phone_location_changed -- only the latter is logged.
        _world({"type": "geofence_arrive_home", "zone": "home", "at": "2026-07-20T10:00:00+00:00"})

        assert _activity_lines() == []


class TestWorldActivityFlapDebounceMatrix:
    def test_repeat_room_entry_within_debounce_window_is_suppressed(self):
        _world({"type": "presence_cleared", "at": "2026-07-20T10:00:00+00:00"})
        _world({"type": "room_entry", "at": "2026-07-20T10:35:00+00:00", "classification": "guest"})
        assert len(_activity_lines()) == 1

        # Guest leaves and re-enters 3 minutes later -- routine flap, well
        # inside the 10-minute default debounce window for the SAME type.
        _world({"type": "presence_cleared", "at": "2026-07-20T10:36:00+00:00"})
        _world({"type": "room_entry", "at": "2026-07-20T10:38:00+00:00", "classification": "guest"})

        assert len(_activity_lines()) == 1  # still just the first one

    def test_repeat_after_debounce_window_elapses_is_logged_again(self):
        _world({"type": "presence_cleared", "at": "2026-07-20T10:00:00+00:00"})
        _world({"type": "room_entry", "at": "2026-07-20T10:35:00+00:00", "classification": "guest"})
        assert len(_activity_lines()) == 1

        # Long vacancy again, and this time outside the 10-minute debounce.
        _world({"type": "presence_cleared", "at": "2026-07-20T10:36:00+00:00"})
        _world({"type": "room_entry", "at": "2026-07-20T11:10:00+00:00", "classification": "guest"})

        assert len(_activity_lines()) == 2

    def test_debounce_window_is_configurable(self, tmp_path, monkeypatch):
        import yaml
        from hermes_constants import get_hermes_home

        config_path = get_hermes_home() / "config.yaml"
        config_path.write_text(
            yaml.safe_dump({"smart_room": {"activity": {"debounce_minutes": 1}}}),
            encoding="utf-8",
        )

        _world({"type": "device_offline", "device": "esp32", "at": "2026-07-20T10:00:00+00:00"})
        _world({"type": "device_offline", "device": "esp32", "at": "2026-07-20T10:00:30+00:00"})
        assert len(_activity_lines()) == 1  # 30s < 1 minute debounce

        _world({"type": "device_offline", "device": "esp32", "at": "2026-07-20T10:02:00+00:00"})
        assert len(_activity_lines()) == 2  # 2 min later, outside the 1-min window

    def test_different_event_types_are_not_cross_debounced(self):
        _world({"type": "device_offline", "device": "esp32", "at": "2026-07-20T10:00:00+00:00"})
        _world({"type": "phone_location_changed", "transition": "arrive", "zone": "home", "at": "2026-07-20T10:00:05+00:00"})

        assert len(_activity_lines()) == 2


class TestWorldActivityRotationFairness:
    def _write_activity(self, records):
        path = get_hermes_home() / "subconscious" / "activity.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    def test_one_chatty_source_cannot_evict_others(self):
        records = [{"at": f"w{i}", "source": "world", "outcome": "diff_silent"} for i in range(450)]
        for i, other_source in enumerate(["reflection", "goblin", "distiller", "tick"] * 13):
            records.append({"at": f"m{i}", "source": other_source, "outcome": "message"})
        records = records[:500]  # 450 world + 50 mixed, at the pre-rotation cap

        self._write_activity(records)

        # One more world entry pushes the file to 501 lines, triggering rotation.
        _world({"type": "device_offline", "device": "esp32", "at": "2026-07-20T10:00:00+00:00"})

        lines = _activity_lines()
        by_source = {}
        for line in lines:
            by_source.setdefault(line["source"], 0)
            by_source[line["source"]] += 1

        assert by_source.get("reflection", 0) > 0
        assert by_source.get("goblin", 0) > 0
        assert by_source.get("distiller", 0) > 0
        assert by_source.get("tick", 0) > 0
        # None of the pre-existing mixed-source entries were evicted.
        assert sum(v for k, v in by_source.items() if k != "world") == 50
        assert by_source["world"] <= scheduler._subconscious_activity_max_per_source()
        assert len(lines) <= scheduler._SUBCONSCIOUS_ACTIVITY_MAX_LINES

    def test_max_per_source_is_configurable(self, monkeypatch):
        import yaml

        config_path = get_hermes_home() / "config.yaml"
        config_path.write_text(
            yaml.safe_dump({"subconscious": {"activity": {"max_per_source": 5}}}),
            encoding="utf-8",
        )

        records = [{"at": f"w{i}", "source": "world", "outcome": "diff_silent"} for i in range(600)]
        self._write_activity(records)

        scheduler._rotate_subconscious_activity(get_hermes_home() / "subconscious" / "activity.jsonl")

        lines = _activity_lines()
        assert len(lines) == 5

    def test_select_rotation_keep_lines_preserves_chronological_order(self):
        lines = [json.dumps({"source": "world", "at": i}) for i in range(10)]
        lines += [json.dumps({"source": "reflection", "at": i}) for i in range(3)]

        kept = scheduler._select_rotation_keep_lines(lines, total_cap=6, max_per_source=3)
        parsed = [json.loads(line) for line in kept]

        # 3 newest "world" + 3 "reflection" = 6, chronological order preserved.
        assert [p["source"] for p in parsed] == ["world", "world", "world", "reflection", "reflection", "reflection"]
        assert [p["at"] for p in parsed if p["source"] == "world"] == [7, 8, 9]
