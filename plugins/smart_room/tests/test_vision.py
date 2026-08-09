from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from plugins.smart_room.runtime.cognition import CognitionWorker, should_reason
from plugins.smart_room.runtime.models import RoomState
from plugins.smart_room.runtime.vision.faces import FaceLibrary, cosine_similarity
from plugins.smart_room.runtime.vision.reasoning import GestureController, SleepTracker, locate_zone


def test_face_similarity_and_reviewed_matching(tmp_path, monkeypatch):
    import plugins.smart_room.runtime.vision.faces as faces_module

    monkeypatch.setattr(faces_module, "get_hermes_home", lambda: tmp_path)
    library = FaceLibrary({"min_enrollment_samples": 3, "match_threshold": 0.7, "ambiguity_margin": 0.05})
    library.enroll("Shereef", [[1, 0], [0.99, 0.01], [0.98, 0.02]], owner=True)
    assert cosine_similarity([1, 0], [1, 0]) == 1
    match = library.match([0.999, 0.001])
    assert match["identity"] == "Shereef"
    assert match["is_owner"] is True
    assert library.match([0, 1])["identity"] == "unknown"


def test_zone_and_sleep_temporal_contract():
    zones = {"bed": [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]}
    assert locate_zone((0.2, 0.5), zones) == "bed"
    assert locate_zone((0.2, 0.5), {"bed": json.dumps(zones["bed"])}) == "bed"
    tracker = SleepTracker({"settling_seconds": 10, "likely_sleeping_seconds": 30, "stillness_distance": 0.1})
    assert tracker.update(owner_visible=True, owner_zone="bed", posture="lying", center=(0.2, 0.5), mmwave_occupied=True, now_monotonic=100) == "in_bed_awake"
    assert tracker.update(owner_visible=True, owner_zone="bed", posture="lying", center=(0.2, 0.5), mmwave_occupied=True, now_monotonic=111) == "settling"
    assert tracker.update(owner_visible=True, owner_zone="bed", posture="lying", center=(0.2, 0.5), mmwave_occupied=True, now_monotonic=131) == "likely_sleeping"
    assert tracker.update(owner_visible=False, owner_zone="unknown", posture="unknown", center=None, mmwave_occupied=True, now_monotonic=140) == "sleeping"


def test_gestures_require_hold_and_explicit_arming():
    gestures = GestureController({"hold_seconds": 0.5, "armed_seconds": 8, "confidence": 0.6})
    assert gestures.update("Thumb_Up", 0.9, now_monotonic=1).command is None
    assert gestures.update("Thumb_Up", 0.9, now_monotonic=2).command is None
    gestures.update("Open_Palm", 0.9, now_monotonic=3)
    armed = gestures.update("Open_Palm", 0.9, now_monotonic=4)
    assert armed.command == "gesture_armed"
    gestures.update("Thumb_Up", 0.9, now_monotonic=5)
    command = gestures.update("Thumb_Up", 0.9, now_monotonic=6)
    assert command.command == "brightness_up"
    assert GestureController({"enabled": False}).update("Open_Palm", 1, now_monotonic=10).command is None


def test_direct_gestures_tolerate_one_dropped_frame():
    gestures = GestureController({
        "hold_seconds": 0.2,
        "confidence": 0.55,
        "gap_tolerance_seconds": 0.25,
        "require_arming": False,
    })
    assert gestures.update("Pointing_Up", 0.9, now_monotonic=1).command is None
    assert gestures.update("", 0.0, now_monotonic=1.1).command is None
    assert gestures.update("Pointing_Up", 0.9, now_monotonic=1.21).command == "toggle_light"


def test_cognition_trigger_filter():
    assert should_reason({"type": "he20_occupied"})
    assert should_reason({"type": "vision_identity_state"})
    assert not should_reason({"type": "light_changed"})


def test_cognition_tool_surface_is_room_restricted(tmp_path, monkeypatch):
    import plugins.smart_room.runtime.cognition as cognition_module
    import plugins.smart_room.runtime.vision.history as history_module

    monkeypatch.setattr(history_module, "get_hermes_home", lambda: tmp_path)
    runtime = SimpleNamespace(
        _config={"vision": {}},
        set_light=lambda **kwargs: {"success": True, "args": kwargs},
        set_mode=lambda mode, reason: None,
    )
    vision = SimpleNamespace(observe=lambda **kwargs: {"person_count": 1}, history=lambda **kwargs: [])
    worker = CognitionWorker({"enabled": True}, runtime, vision)
    assert worker._execute("observe_room", {})["vision"]["person_count"] == 1
    result = worker._execute("set_light", {"on": True, "brightness": 8, "purpose": "final", "unexpected": "ignored"})
    assert result["args"] == {"on": True, "brightness": 8, "manual": False}
    try:
        worker._execute("terminal", {})
    except ValueError as exc:
        assert "unknown cognition tool" in str(exc)
    else:
        raise AssertionError("non-room tool should be rejected")


def test_cognition_runs_bounded_tool_loop_and_audits_decision(tmp_path, monkeypatch):
    import agent.auxiliary_client as auxiliary
    import plugins.smart_room.runtime.vision.history as history_module

    monkeypatch.setattr(history_module, "get_hermes_home", lambda: tmp_path)
    calls = []

    def tool_call(call_id, name, args):
        return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))

    responses = iter([
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None,
            reasoning_content="The room is dark, so inspect at low warm light.",
            tool_calls=[tool_call("a", "set_light", {"on": True, "brightness": 8, "color_temp": 2200, "purpose": "inspection"})],
        ))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None,
            reasoning_content="The room is empty; restore darkness.",
            tool_calls=[tool_call("b", "set_light", {"on": False, "purpose": "final"}), tool_call("c", "remain_silent", {"reason": "false positive"})],
        ))]),
    ])
    monkeypatch.setattr(auxiliary, "call_llm", lambda **kwargs: (calls.append(kwargs), next(responses))[1])
    state = RoomState()
    state.mmwave.occupied = True
    runtime_events = []
    runtime = SimpleNamespace(
        _config={"vision": {}}, _state=state, _state_lock=threading.RLock(),
        set_light=lambda **kwargs: {"success": True, "args": kwargs},
        set_mode=lambda mode, reason: None,
        _emit_event=lambda event_type, data: runtime_events.append((event_type, data)),
    )
    vision = SimpleNamespace(observe=lambda **kwargs: {"visibility": "good", "person_count": 0}, history=lambda **kwargs: [])
    worker = CognitionWorker({"enabled": True, "provider": "deepseek", "model": "deepseek-v4-flash"}, runtime, vision)
    worker._decide({"type": "he20_occupied", "at": "2026-08-09T00:00:00Z"})
    assert len(calls) == 2
    assert [message["role"] for message in calls[1]["messages"][-3:]] == ["assistant", "tool", "tool"]
    assert runtime_events[-1][0] == "smart_room_cognition"
    assert [action["tool"] for action in runtime_events[-1][1]["actions"]] == ["set_light", "set_light", "remain_silent"]


def test_cognition_rolls_back_pre_llm_reflex_if_not_committed(tmp_path, monkeypatch):
    import agent.auxiliary_client as auxiliary
    import plugins.smart_room.runtime.vision.history as history_module

    monkeypatch.setattr(history_module, "get_hermes_home", lambda: tmp_path)

    def tool_call(call_id, name, args):
        return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))

    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=None,
        tool_calls=[tool_call("a", "remain_silent", {"reason": "false positive"})],
    ))])
    monkeypatch.setattr(auxiliary, "call_llm", lambda **kwargs: response)
    state = RoomState()
    state.light.on = True
    state.light.brightness = 8
    light_calls = []
    runtime = SimpleNamespace(
        _config={"vision": {}}, _state=state, _state_lock=threading.RLock(),
        set_light=lambda **kwargs: (light_calls.append(kwargs), {"success": True})[1],
        set_mode=lambda mode, reason: None,
        _emit_event=lambda event_type, data: None,
    )
    worker = CognitionWorker({"enabled": True}, runtime, SimpleNamespace())
    worker._decide({
        "type": "he20_occupied",
        "reflex": {
            "applied": True,
            "restore": {"on": False, "brightness": 0, "color_temp": 3000, "rgb": None},
        },
    })
    assert light_calls[-1] == {
        "on": False,
        "brightness": 0,
        "color_temp": 3000,
        "rgb": None,
        "manual": False,
    }
