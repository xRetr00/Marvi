from __future__ import annotations

import logging
from datetime import datetime, timezone

from agent.learning import escalation, focus_apps, outcomes, reflection, room_habit, timing, trust, voice_tuning
from agent.learning.registry import validate_config_spec


def test_outcome_ledger_records_filters_and_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(outcomes, "get_hermes_home", lambda: tmp_path)
    assert outcomes.record("trust", "calendar", "accepted", ref="one")
    assert outcomes.record("trust", "mail", "dismissed", ref="two")
    assert outcomes.recent(loop="trust", category="calendar")[0]["ref"] == "one"
    assert len(outcomes.recent("trust", None, None, 1)) == 1
    assert outcomes.counts("trust", "calendar", 30)["accepted"] == 1
    assert outcomes.record("not-a-loop", "general", "observed") is None


def test_outcome_log_contains_metadata_not_private_detail(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(outcomes, "get_hermes_home", lambda: tmp_path)

    with caplog.at_level(logging.INFO, logger=outcomes.__name__):
        outcomes.record(
            "escalation",
            "voice",
            "corrected",
            ref="private-reference",
            detail={"utterance": "private remembered text"},
        )

    assert "loop=escalation" in caplog.text
    assert "event=corrected" in caplog.text
    assert "private-reference" not in caplog.text
    assert "private remembered text" not in caplog.text


def test_registry_rejects_unknown_and_out_of_bounds_paths():
    try:
        validate_config_spec({"path": "agent.max_turns", "value": 1, "current": 90, "rationale": "x"})
    except ValueError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("arbitrary config path was accepted")

    try:
        validate_config_spec({"path": "voice.speaker_id.threshold", "value": .29, "current": .45, "rationale": "x"})
    except ValueError as exc:
        assert "range" in str(exc)
    else:
        raise AssertionError("out-of-range threshold was accepted")


def test_trust_promotes_demotes_and_excludes():
    accepts = [{"event": "accepted", "category": "calendar", "detail": {"accepted_by": "user"}} for _ in range(8)]
    assert trust.proposals(accepts, {"calendar": "propose"})[0]["value"] == "auto"
    assert trust.proposals([{"event": "dismissed", "category": "calendar"}, *accepts], {"calendar": "propose"}) == []
    goals = [{**row, "category": "goal"} for row in accepts]
    assert trust.proposals(goals, {"goal": "propose"}) == []
    dismissals = [{"event": "dismissed", "category": "mail"} for _ in range(3)]
    assert trust.proposals(dismissals, {"mail": "auto"})[0]["value"] == "notify"


def test_reflection_reproduces_trust_config_proposal_from_fixture_ledger(monkeypatch):
    ledger = [{"event": "accepted", "category": "calendar", "detail": {"accepted_by": "user"}} for _ in range(8)]
    captured = []
    cfg = {
        "learning": {
            "trust": {"enabled": True, "review_weekday": 6},
            "room": {"enabled": False},
            "focus_apps": {"enabled": False},
            "voice_tuning": {"enabled": False},
            "escalation": {"enabled": False},
            "timing": {"enabled": False},
        }
    }
    monkeypatch.setattr(reflection, "load_config", lambda: cfg)
    monkeypatch.setattr(reflection, "recent", lambda *args, **kwargs: ledger)
    monkeypatch.setattr(reflection, "get_tiers_config", lambda: {"calendar": "propose"})
    monkeypatch.setattr(reflection, "current_value", lambda path: "propose")
    monkeypatch.setattr(reflection, "add_suggestion", lambda **kwargs: captured.append(kwargs) or kwargs)

    result = reflection.run_reflection(datetime(2026, 7, 19, tzinfo=timezone.utc))
    assert result["proposals"] == 1
    assert captured[0]["kind"] == "config"
    assert captured[0]["config_spec"]["path"] == "subconscious.tiers.calendar"
    assert captured[0]["loop"] == "trust"


def test_reflection_logs_start_and_compact_completion(monkeypatch, caplog):
    monkeypatch.setattr(
        reflection,
        "load_config",
        lambda: {
            "learning": {
                "room": {"enabled": False},
                "focus_apps": {"enabled": False},
                "trust": {"enabled": False},
                "voice_tuning": {"enabled": False},
                "escalation": {"enabled": False},
                "timing": {"enabled": False},
            }
        },
    )

    with caplog.at_level(logging.INFO, logger=reflection.__name__):
        result = reflection.run_reflection(datetime(2026, 7, 19, tzinfo=timezone.utc))

    assert result == {"proposals": 0, "samples": {}}
    assert "learning reflection started" in caplog.text
    assert "learning reflection completed proposals=0 samples={}" in caplog.text


def _room_event(event_id, at, kind, **extra):
    return {"id": event_id, "at": at, "type": kind, "source": "manual", **extra}


def test_room_histogram_manual_filter_threshold_variance_and_cancellation():
    events = []
    for week in range(4):
        # Four Mondays around 15:00.
        events.append(_room_event(week + 1, f"2026-07-{6 + week * 7:02d}T15:0{week}:00+00:00", "mode_changed", mode="focus"))
    events.append({"id": 10, "at": "2026-07-06T15:00:00+00:00", "type": "mode_changed", "source": "automation", "mode": "relax"})
    state = room_habit.accumulate(events)
    proposals = room_habit.propose(state, minimum_occurrences=4)
    assert len(state["observations"]) == 4
    assert proposals[0]["job_spec"]["enabled_toolsets"] == ["smart_room"]
    inconsistent = room_habit.accumulate([
        _room_event(30 + week, f"2026-07-{6 + week * 7:02d}T{10 + week * 2:02d}:00:00+00:00", "mode_changed", mode="reading")
        for week in range(4)
    ])
    assert room_habit.propose(inconsistent, minimum_occurrences=4) == []

    cancellation = [
        _room_event(20 + week, f"2026-07-{10 + week * 7:02d}T21:00:00+00:00", "sleep_cancelled", reason="evening")
        for week in range(4)
    ]
    cancellation_state = room_habit.accumulate(cancellation)
    assert "smart_room_cancel_sleep" in room_habit.propose(cancellation_state)[0]["job_spec"]["prompt"]


def test_voice_parser_proposal_and_minimum_sample_gate():
    lines = [f"INFO [VOICE-ID] context=utterance zone=OWNER label=owner score=0.55 audio_ms=900 resolved_by=score ignored=false" for _ in range(180)]
    lines += [f"INFO [VOICE-ID] context=utterance zone=ABSTAIN label=owner score=0.39 audio_ms=900 resolved_by=continuity ignored=false" for _ in range(20)]
    lines += ["INFO [VOICE-ID] context=barge zone=ABSTAIN label=owner score=0.1 resolved_by=continuity ignored=false"]
    stats = voice_tuning.analyze(lines)
    assert stats["samples"] == 200
    proposal = voice_tuning.propose_threshold(stats, {"threshold": .45, "reject_threshold": .25})
    assert proposal and proposal["path"] == "voice.speaker_id.threshold"
    assert voice_tuning.propose_threshold({**stats, "samples": 199}, {"threshold": .45, "reject_threshold": .25}) is None


def test_focus_apps_derives_only_repeated_long_unknown_apps():
    events = [{"duration": 26 * 60, "data": {"app": "WriterPro"}} for _ in range(5)]
    events += [{"duration": 60, "data": {"app": "Tiny"}} for _ in range(20)]
    proposal = focus_apps.derive(events, ["Code"])
    assert proposal and proposal["value"] == ["Code", "WriterPro"]
    assert focus_apps.derive(events, ["writer"]) is None

    split_events = []
    for day in range(5):
        split_events.extend([
            {"timestamp": f"2026-07-{day + 1:02d}T09:00:00Z", "duration": 15 * 60, "data": {"app": "Studio"}},
            {"timestamp": f"2026-07-{day + 1:02d}T09:15:30Z", "duration": 15 * 60, "data": {"app": "Studio"}},
        ])
    assert focus_apps.derive(split_events, ["Code"])["value"][-1] == "Studio"


def test_escalation_correction_and_hint_caps(tmp_path, monkeypatch):
    assert escalation.is_correction("book a table for two", "Sure, where?", "Actually, make that four")
    assert not escalation.is_correction("what time is it", "It is noon", "thank you")
    rows = [
        {"event": "corrected", "detail": {"prior_utterance": f"compare all the options for project {index}"}}
        for index in range(10)
    ]
    block = escalation.mine(rows)
    assert block.count("\n-") <= 5
    assert len(block) <= 600
    monkeypatch.setattr(escalation, "get_hermes_home", lambda: tmp_path)
    assert escalation.write_hints([]) == ""
    assert not escalation.hints_path().exists()
    escalation.write_hints(rows)
    assert escalation.hints_path().exists()


def test_timing_requires_matched_engagement_and_finds_quiet_hour():
    delivery_only = [
        {"at": f"2026-07-{1 + index // 24:02d}T{index % 24:02d}:00:00+00:00", "event": "delivered", "ref": f"d{index}"}
        for index in range(120)
    ]
    assert timing.propose_windows(delivery_only, minimum_deliveries=100) is None

    rows = []
    # 110 engaged daytime deliveries satisfy the evidence gate.
    for index in range(110):
        ref = f"day-{index}"
        rows.extend([
            {"at": "2026-07-01T12:00:00+00:00", "event": "delivered", "ref": ref},
            {"at": "2026-07-01T12:10:00+00:00", "event": "engaged", "ref": ref},
        ])
    # Night sends are unmatched, creating a supported quiet window.
    rows.extend({"at": "2026-07-01T03:00:00+00:00", "event": "delivered", "ref": f"night-{i}"} for i in range(12))
    proposal = timing.propose_windows(rows, minimum_deliveries=100)
    assert proposal and len(proposal["value"]) == 1


def test_timing_signal_is_a_noop_while_disabled(monkeypatch):
    called = []
    monkeypatch.setattr(timing, "_settings", lambda: (False, 60))
    monkeypatch.setattr(timing, "record", lambda *args, **kwargs: called.append((args, kwargs)))

    timing.record_delivery(platform="telegram", chat_id="1")
    assert called == []
