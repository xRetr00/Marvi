from __future__ import annotations

import pytest

from plugins.smart_room.runtime import sound_events
from plugins.smart_room.runtime.app import Runtime
from plugins.smart_room.runtime.clap_dataset import ClapDataset
from plugins.smart_room.runtime.sound_events import ClapSequence


def test_single_clap_is_ignored_and_double_clap_toggles() -> None:
    actions = []
    sequence = ClapSequence(actions.append, max_gap=0.9, decision_delay=0.6, cooldown=3)

    sequence.add(1.0)
    sequence.tick(1.91)
    assert actions == []

    sequence.add(3.0)
    sequence.add(3.4)
    sequence.tick(4.01)
    assert actions == ["toggle_light"]


def test_one_ringing_clap_is_not_counted_twice() -> None:
    actions = []
    sequence = ClapSequence(actions.append, min_gap=0.3, max_gap=0.9, decision_delay=0.6)

    assert sequence.add(1.0)
    assert not sequence.add(1.25)
    sequence.tick(1.91)

    assert actions == []


def test_triple_clap_enters_sleep_and_cooldown_suppresses_retrigger() -> None:
    actions = []
    sequence = ClapSequence(actions.append, max_gap=0.9, decision_delay=0.7, cooldown=3)

    assert sequence.add(1.0)
    assert sequence.add(1.3)
    assert sequence.add(1.6)
    assert actions == ["sleep"]
    assert not sequence.add(2.0)
    sequence.tick(5.0)
    assert actions == ["sleep"]


def test_speech_suppression_discards_pending_claps() -> None:
    actions = []
    sequence = ClapSequence(actions.append, max_gap=0.9, decision_delay=0.6, cooldown=3)

    assert sequence.add(1.0)
    sequence.suppress(1.2, duration=2.5)
    assert not sequence.add(1.5)
    assert not sequence.add(3.6)
    assert sequence.add(3.8)
    sequence.tick(4.71)

    assert actions == []


def test_bad_model_download_is_rejected(monkeypatch, tmp_path) -> None:
    source = tmp_path / "not-yamnet.tflite"
    source.write_bytes(b"not the pinned model")
    monkeypatch.setattr(sound_events, "get_hermes_home", lambda: tmp_path / "profile")
    monkeypatch.setattr(sound_events, "_MODEL_URL", source.as_uri())

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        sound_events._ensure_model()

    assert not (tmp_path / "profile/smart_room/models/yamnet_clap_quantized.tflite").exists()


def test_clap_score_accepts_the_yamnet_hand_sound_family() -> None:
    class Scores:
        def reshape(self, _size):
            values = [0.0] * 521
            values[57] = 0.3  # A close microphone often calls a clap a finger snap.
            return values

    assert sound_events._clap_score(Scores()) == 0.3


def test_model_accepts_only_yamnet_hand_sound_confidence() -> None:
    class Scores:
        def __init__(self, clap=0.0):
            self.values = [0.0] * 521
            self.values[400] = 0.99  # An unrelated sharp sound must not pass.
            self.values[58] = clap

        def reshape(self, _size):
            return self.values

    assert not sound_events._model_accepts_transient(Scores(), 0.15)
    assert not sound_events._model_accepts_transient(Scores(clap=0.14), 0.15)
    assert sound_events._model_accepts_transient(Scores(clap=0.15), 0.15)


def test_transient_gate_adapts_to_microphone_noise_without_accepting_broad_audio() -> None:
    accepted, threshold, crest = sound_events._transient_gate(0.05, 0.015, 0.01, {})
    assert accepted
    assert threshold == pytest.approx(0.04)
    assert crest > 3

    accepted, _, crest = sound_events._transient_gate(0.05, 0.03, 0.01, {})
    assert not accepted
    assert crest < 2


def test_sound_actions_use_room_state_and_tuya_status() -> None:
    class Tuya:
        def __init__(self) -> None:
            self.commands = []

        def get_light_status(self):
            return {"success": True, "on": True}

        def set_light(self, **kwargs):
            self.commands.append(kwargs)
            return {"success": True}

    runtime = Runtime({})
    runtime._tuya = Tuya()

    runtime._on_sound_action("toggle_light")
    assert runtime._tuya.commands[-1]["on"] is False

    runtime._on_sound_action("sleep")
    assert runtime._state.modes.active_mode == "sleep"
    assert runtime._tuya.commands[-1]["on"] is False


def test_triple_clap_sleep_is_safe_by_default() -> None:
    actions = []
    listener = sound_events.SoundEventListener({"enabled": True}, actions.append)

    listener._dispatch_action("sleep")

    assert actions == []
    assert listener.status()["last_action"] is None


def test_clap_dataset_requires_human_confirmation_and_tracks_200_target(tmp_path) -> None:
    dataset = ClapDataset(tmp_path)
    first = dataset.record([0.0, 0.5, -0.5], score=0.8)
    second = dataset.record([0.0, 0.2, -0.2], score=0.7)

    pending = dataset.status()
    assert pending["confirmed"] == 0
    assert pending["pending"] == 2
    assert pending["target"] == 200
    assert pending["next_pending"]["id"] == second["id"]

    accepted = dataset.review(first["id"], True)
    rejected = dataset.review(second["id"], False)

    assert accepted["confirmed"] == 1
    assert rejected["confirmed"] == 1
    assert rejected["rejected"] == 1
    assert rejected["remaining"] == 199
    assert rejected["next_pending"] is None
    assert len(list((tmp_path / "samples").glob("*.wav"))) == 2
