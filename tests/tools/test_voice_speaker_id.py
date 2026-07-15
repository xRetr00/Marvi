"""Tests for tools/voice_speaker_id.py: store CRUD, cosine similarity/
threshold matching, and owner/guest/unknown resolution -- all with canned
vectors, no sherpa-onnx or network access.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from tools import voice_speaker_id as vsid


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "voice" / "speakers.json"


# ---------------------------------------------------------------------------
# Store CRUD
# ---------------------------------------------------------------------------


class TestStoreCrud:
    def test_load_store_missing_file_returns_empty(self, store_path):
        store = vsid.load_store(store_path)
        assert store == {"owner": None, "speakers": {}}

    def test_enroll_embedding_creates_store(self, store_path):
        store = vsid.enroll_embedding("Alice", [1.0, 0.0, 0.0], path=store_path)

        assert store_path.exists()
        assert store["owner"] == "alice"
        assert store["speakers"]["alice"]["display_name"] == "Alice"
        assert store["speakers"]["alice"]["embeddings"] == [[1.0, 0.0, 0.0]]

    def test_first_enrolled_name_becomes_owner(self, store_path):
        vsid.enroll_embedding("Bob", [0.0, 1.0, 0.0], path=store_path)
        store = vsid.enroll_embedding("Alice", [1.0, 0.0, 0.0], path=store_path)

        assert store["owner"] == "bob"

    def test_literal_owner_name_always_claims_owner_slot(self, store_path):
        vsid.enroll_embedding("Bob", [0.0, 1.0, 0.0], path=store_path)
        store = vsid.enroll_embedding("owner", [1.0, 0.0, 0.0], path=store_path)

        assert store["owner"] == "owner"

    def test_multiple_embeddings_per_name_are_appended(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        store = vsid.enroll_embedding("Alice", [0.9, 0.1], path=store_path)

        assert store["speakers"]["alice"]["embeddings"] == [[1.0, 0.0], [0.9, 0.1]]

    def test_enroll_embedding_requires_name(self, store_path):
        with pytest.raises(ValueError):
            vsid.enroll_embedding("   ", [1.0], path=store_path)

    def test_enroll_embedding_requires_embedding(self, store_path):
        with pytest.raises(ValueError):
            vsid.enroll_embedding("Alice", [], path=store_path)

    def test_list_speakers(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        vsid.enroll_embedding("Bob", [0.0, 1.0], path=store_path)
        vsid.enroll_embedding("Bob", [0.0, 0.9], path=store_path)

        speakers = {s["name"]: s for s in vsid.list_speakers(path=store_path)}
        assert speakers["Alice"]["is_owner"] is True
        assert speakers["Alice"]["embeddings"] == 1
        assert speakers["Bob"]["is_owner"] is False
        assert speakers["Bob"]["embeddings"] == 2

    def test_profile_is_ready_after_three_consistent_samples(self, store_path):
        for embedding in ([1.0, 0.0], [0.99, 0.05], [0.98, -0.04]):
            vsid.enroll_embedding("Alice", embedding, path=store_path)

        speaker = vsid.list_speakers(path=store_path)[0]
        assert speaker["ready"] is True
        assert speaker["samples_needed"] == 0
        assert speaker["consistency"] > 0.9

    def test_remove_speaker(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        vsid.enroll_embedding("Bob", [0.0, 1.0], path=store_path)

        assert vsid.remove_speaker("bob", path=store_path) is True
        assert vsid.remove_speaker("bob", path=store_path) is False
        assert [s["name"] for s in vsid.list_speakers(path=store_path)] == ["Alice"]

    def test_remove_owner_promotes_next_speaker(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        vsid.enroll_embedding("Bob", [0.0, 1.0], path=store_path)

        vsid.remove_speaker("alice", path=store_path)
        store = vsid.load_store(store_path)
        assert store["owner"] == "bob"

    def test_remove_last_speaker_clears_owner(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        vsid.remove_speaker("alice", path=store_path)
        store = vsid.load_store(store_path)
        assert store["owner"] is None

    def test_atomic_write_produces_valid_json(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        data = json.loads(store_path.read_text(encoding="utf-8"))
        assert data["owner"] == "alice"

    def test_corrupt_store_file_treated_as_empty(self, store_path):
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text("not json{{{", encoding="utf-8")
        assert vsid.load_store(store_path) == {"owner": None, "speakers": {}}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode bits are not meaningful on Windows")
    def test_store_file_written_0600(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        mode = stat.S_IMODE(os.stat(store_path).st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# Cosine similarity + matching
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self):
        assert vsid.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self):
        assert vsid.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_score_negative_one(self):
        assert vsid.cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_mismatched_dims_returns_zero(self):
        assert vsid.cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0

    def test_empty_vector_returns_zero(self):
        assert vsid.cosine_similarity([], [1.0]) == 0.0

    def test_zero_vector_returns_zero(self):
        assert vsid.cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


class TestIdentifyEmbedding:
    def test_owner_match_above_threshold(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0, 0.0], path=store_path)

        label, score = vsid.identify_embedding([1.0, 0.0, 0.0], threshold=0.45, path=store_path)
        assert label == "owner"
        assert score == pytest.approx(1.0)

    def test_guest_match_above_threshold(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0, 0.0], path=store_path)  # owner
        vsid.enroll_embedding("Bob", [0.0, 1.0, 0.0], path=store_path)  # guest

        label, score = vsid.identify_embedding([0.0, 1.0, 0.0], threshold=0.45, path=store_path)
        assert label == "guest"
        assert score == pytest.approx(1.0)

    def test_details_include_enrolled_display_name(self, store_path):
        vsid.enroll_embedding("Alice Smith", [1.0, 0.0], path=store_path)

        label, score, name = vsid.identify_embedding_details(
            [1.0, 0.0], threshold=0.45, path=store_path
        )
        assert (label, name) == ("owner", "Alice Smith")
        assert score == pytest.approx(1.0)

    def test_below_threshold_is_unknown(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0, 0.0], path=store_path)

        # Near-orthogonal probe -- low cosine similarity.
        label, score = vsid.identify_embedding([0.0, 1.0, 0.0], threshold=0.45, path=store_path)
        assert label == "unknown"
        assert score < 0.45

    def test_empty_store_is_unknown(self, store_path):
        label, score = vsid.identify_embedding([1.0, 0.0], threshold=0.45, path=store_path)
        assert label == "unknown"
        assert score == 0.0

    def test_averages_multiple_enrolled_embeddings(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        vsid.enroll_embedding("Alice", [0.6, 0.8], path=store_path)

        # Average of [1,0] and [0.6,0.8] is [0.8, 0.4]; probing with that
        # exact average should score ~1.0 (best possible match).
        label, score = vsid.identify_embedding([0.8, 0.4], threshold=0.45, path=store_path)
        assert label == "owner"
        assert score == pytest.approx(1.0, abs=1e-6)

    def test_threshold_boundary_is_inclusive(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        label, score = vsid.identify_embedding([1.0, 0.0], threshold=1.0, path=store_path)
        assert label == "owner"


class TestIdentify:
    def test_missing_store_returns_unknown_without_raising(self, store_path):
        label, score = vsid.identify(b"\x00\x00" * 100, path=store_path)
        assert (label, score) == ("unknown", 0.0)

    def test_compute_embedding_failure_returns_unknown(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: None)

        label, score = vsid.identify(b"\x00\x00" * 100, path=store_path)
        assert (label, score) == ("unknown", 0.0)

    def test_identify_uses_computed_embedding(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: [1.0, 0.0])

        label, score = vsid.identify(b"\x00\x00" * 100, cfg={}, path=store_path)
        assert label == "owner"
        assert score == pytest.approx(1.0)

    def test_never_raises_on_unexpected_error(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)

        def _boom(*a, **k):
            raise RuntimeError("sherpa exploded")

        monkeypatch.setattr(vsid, "compute_embedding", _boom)
        label, score = vsid.identify(b"\x00\x00" * 100, path=store_path)
        assert (label, score) == ("unknown", 0.0)


def test_warm_loads_runtime_before_enrolled_model(store_path, monkeypatch):
    store_path.parent.mkdir(parents=True)
    store_path.write_text('{"owner": null, "speakers": {}}', encoding="utf-8")
    events = []
    monkeypatch.setattr(vsid, "default_store_path", lambda: store_path)
    monkeypatch.setattr(vsid, "_import_sherpa_onnx", lambda: events.append("runtime"))
    monkeypatch.setattr(vsid, "resolve_speaker_model_path", lambda cfg=None: events.append("model") or "model.onnx")
    monkeypatch.setattr(vsid, "_get_extractor", lambda path: events.append("extractor"))

    assert vsid.warm_speaker_id({}) is True
    assert events == ["runtime", "model", "extractor"]


# ---------------------------------------------------------------------------
# Enroll (transport-facing, mocked)
# ---------------------------------------------------------------------------


class TestEnroll:
    def test_enroll_computes_embedding_and_stores_it(self, store_path, monkeypatch):
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: [1.0, 0.0])

        store = vsid.enroll("Alice", b"\x00\x00" * 100, path=store_path)
        assert store["speakers"]["alice"]["embeddings"] == [[1.0, 0.0]]

    def test_enroll_raises_when_embedding_unavailable(self, store_path, monkeypatch):
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: None)

        with pytest.raises(vsid.SpeakerIdUnavailable):
            vsid.enroll("Alice", b"\x00\x00" * 100, path=store_path)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


class TestConfig:
    def test_focus_mode_setting_defaults_owner(self):
        assert vsid.focus_mode_setting({}) == "owner"

    def test_focus_mode_setting_respects_config(self):
        cfg = {"voice": {"speaker_id": {"focus_mode": "off"}}}
        assert vsid.focus_mode_setting(cfg) == "off"

    def test_focus_mode_setting_falls_back_on_unknown_value(self):
        cfg = {"voice": {"speaker_id": {"focus_mode": "nonsense"}}}
        assert vsid.focus_mode_setting(cfg) == "owner"

    def test_focus_mode_ready_false_with_no_store(self, store_path):
        assert vsid.focus_mode_ready({}, path=store_path) is False

    def test_focus_mode_ready_false_when_owner_has_no_embeddings(self, store_path):
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text(
            json.dumps({"owner": "alice", "speakers": {"alice": {"display_name": "Alice", "embeddings": []}}}),
            encoding="utf-8",
        )
        assert vsid.focus_mode_ready({}, path=store_path) is False

    def test_focus_mode_ready_true_when_owner_enrolled_and_model_loads(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        monkeypatch.setattr(vsid, "resolve_speaker_model_path", lambda cfg=None: "model.onnx")
        monkeypatch.setattr(vsid, "_get_extractor", lambda path: object())

        assert vsid.focus_mode_ready({}, path=store_path) is True

    def test_focus_mode_ready_false_when_model_load_fails(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)

        def _boom(*a, **k):
            raise RuntimeError("sherpa exploded")

        monkeypatch.setattr(vsid, "resolve_speaker_model_path", _boom)

        assert vsid.focus_mode_ready({}, path=store_path) is False

    def test_focus_mode_active_false_when_setting_off_even_if_ready(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        monkeypatch.setattr(vsid, "resolve_speaker_model_path", lambda cfg=None: "model.onnx")
        monkeypatch.setattr(vsid, "_get_extractor", lambda path: object())
        cfg = {"voice": {"speaker_id": {"focus_mode": "off"}}}

        assert vsid.focus_mode_active(cfg, path=store_path) is False

    def test_focus_mode_active_false_when_not_ready_even_if_owner_setting(self, store_path):
        assert vsid.focus_mode_active({}, path=store_path) is False

    def test_focus_mode_active_true_when_owner_setting_and_ready(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        monkeypatch.setattr(vsid, "resolve_speaker_model_path", lambda cfg=None: "model.onnx")
        monkeypatch.setattr(vsid, "_get_extractor", lambda path: object())

        assert vsid.focus_mode_active({}, path=store_path) is True


# ---------------------------------------------------------------------------
# Three-zone identify (fail-open voice-focus redesign)
# ---------------------------------------------------------------------------


class TestZoneConfig:
    def test_owner_threshold_default(self):
        assert vsid.owner_threshold({}) == pytest.approx(0.45)

    def test_reject_threshold_default(self):
        assert vsid.reject_threshold({}) == pytest.approx(0.25)

    def test_continuity_seconds_default(self):
        assert vsid.continuity_seconds({}) == pytest.approx(120.0)

    def test_competing_window_seconds_default(self):
        assert vsid.competing_window_seconds({}) == pytest.approx(90.0)

    def test_config_overrides_are_honored(self):
        cfg = {
            "voice": {
                "speaker_id": {
                    "threshold": 0.5,
                    "reject_threshold": 0.3,
                    "continuity_seconds": 60,
                    "competing_window_seconds": 30,
                }
            }
        }
        assert vsid.owner_threshold(cfg) == pytest.approx(0.5)
        assert vsid.reject_threshold(cfg) == pytest.approx(0.3)
        assert vsid.continuity_seconds(cfg) == pytest.approx(60.0)
        assert vsid.competing_window_seconds(cfg) == pytest.approx(30.0)


def _long_pcm(seconds: float = 3.0) -> bytes:
    """Enough 16kHz mono PCM16 silence bytes to clear the CONFIDENT_OTHER
    2s clean-audio floor -- content doesn't matter since compute_embedding
    is monkeypatched in these tests, only len() does."""
    n_samples = int(16000 * seconds)
    return b"\x00\x00" * n_samples


def _short_pcm(seconds: float = 0.3) -> bytes:
    n_samples = int(16000 * seconds)
    return b"\x00\x00" * n_samples


class TestIdentifyZoned:
    def test_no_store_is_abstain(self, store_path):
        result = vsid.identify_zoned(_long_pcm(), path=store_path)
        assert result["zone"] == vsid.ZONE_ABSTAIN
        assert result["label"] == "unknown"

    def test_no_owner_enrolled_is_abstain(self, store_path, monkeypatch):
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text('{"owner": null, "speakers": {"bob": {"display_name": "Bob", "embeddings": [[1.0, 0.0]]}}}', encoding="utf-8")
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: [1.0, 0.0])

        result = vsid.identify_zoned(_long_pcm(), path=store_path)
        assert result["zone"] == vsid.ZONE_ABSTAIN

    def test_degraded_audio_is_abstain(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: None)

        result = vsid.identify_zoned(_long_pcm(), cfg={}, path=store_path)
        assert result["zone"] == vsid.ZONE_ABSTAIN
        assert result["label"] == "unknown"

    def test_owner_score_at_or_above_threshold_is_owner_zone(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: [1.0, 0.0])

        result = vsid.identify_zoned(_long_pcm(), cfg={}, path=store_path)
        assert result["zone"] == vsid.ZONE_OWNER
        assert result["label"] == "owner"
        assert result["score"] == pytest.approx(1.0)
        assert result["name"] == "Alice"

    def test_owner_zone_boundary_is_inclusive(self, store_path, monkeypatch):
        # cos(theta) == 0.45 exactly by construction.
        import math

        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        angle = math.acos(0.45)
        probe = [math.cos(angle), math.sin(angle)]
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: probe)

        result = vsid.identify_zoned(_long_pcm(), cfg={}, path=store_path)
        assert result["zone"] == vsid.ZONE_OWNER

    def test_enrolled_guest_above_threshold_is_owner_zone_but_guest_label(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)  # owner
        vsid.enroll_embedding("Bob", [0.0, 1.0], path=store_path)  # guest

        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: [0.0, 1.0])
        result = vsid.identify_zoned(_long_pcm(), cfg={}, path=store_path)
        assert result["zone"] == vsid.ZONE_OWNER
        assert result["label"] == "guest"
        assert result["name"] == "Bob"

    def test_low_score_with_enough_clean_audio_is_confident_other(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: [0.0, 1.0])  # orthogonal, score 0.0

        result = vsid.identify_zoned(_long_pcm(3.0), cfg={}, path=store_path)
        assert result["zone"] == vsid.ZONE_CONFIDENT_OTHER
        assert result["label"] == "unknown"

    def test_low_score_with_short_audio_is_abstain_not_confident_other(self, store_path, monkeypatch):
        """Duration gate: a low score alone isn't enough to reject -- short
        audio could just be weak signal, not necessarily a different voice."""
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: [0.0, 1.0])

        result = vsid.identify_zoned(_short_pcm(0.3), cfg={}, path=store_path)
        assert result["zone"] == vsid.ZONE_ABSTAIN

    def test_confident_other_duration_boundary_is_inclusive(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: [0.0, 1.0])

        result = vsid.identify_zoned(_long_pcm(2.0), cfg={}, path=store_path)
        assert result["zone"] == vsid.ZONE_CONFIDENT_OTHER

    def test_mid_score_is_abstain(self, store_path, monkeypatch):
        import math

        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        angle = math.acos(0.35)  # strictly between 0.25 reject and 0.45 owner
        probe = [math.cos(angle), math.sin(angle)]
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: probe)

        result = vsid.identify_zoned(_long_pcm(3.0), cfg={}, path=store_path)
        assert result["zone"] == vsid.ZONE_ABSTAIN

    def test_model_mismatch_forces_abstain_with_flag(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path, model_id="some-other-model")
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: [1.0, 0.0])

        result = vsid.identify_zoned(_long_pcm(), cfg={}, path=store_path)
        assert result["zone"] == vsid.ZONE_ABSTAIN
        assert result["model_mismatch"] is True

    def test_never_raises_on_unexpected_error(self, store_path, monkeypatch):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)

        def _boom(*a, **k):
            raise RuntimeError("sherpa exploded")

        monkeypatch.setattr(vsid, "compute_embedding", _boom)
        result = vsid.identify_zoned(_long_pcm(), cfg={}, path=store_path)
        assert result["zone"] == vsid.ZONE_ABSTAIN


class TestAdaptiveRing:
    def test_append_adds_to_owner_only(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        vsid.append_adaptive_embedding([0.9, 0.1], path=store_path)

        store = vsid.load_store(store_path)
        assert store["speakers"]["alice"]["adaptive_embeddings"] == [[0.9, 0.1]]

    def test_append_without_owner_is_noop(self, store_path):
        store = vsid.append_adaptive_embedding([0.9, 0.1], path=store_path)
        assert store == {"owner": None, "speakers": {}}

    def test_ring_is_capped_fifo(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        for i in range(vsid.ADAPTIVE_RING_CAP + 5):
            vsid.append_adaptive_embedding([float(i), 0.0], path=store_path)

        store = vsid.load_store(store_path)
        ring = store["speakers"]["alice"]["adaptive_embeddings"]
        assert len(ring) == vsid.ADAPTIVE_RING_CAP
        # Oldest 5 evicted -- ring now starts at index 5.
        assert ring[0] == [5.0, 0.0]
        assert ring[-1] == [float(vsid.ADAPTIVE_RING_CAP + 4), 0.0]

    def test_adaptive_count(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        assert vsid.adaptive_count(path=store_path) == 0
        vsid.append_adaptive_embedding([0.9, 0.1], path=store_path)
        assert vsid.adaptive_count(path=store_path) == 1

    def test_reset_clears_adaptive_but_not_manual(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        vsid.append_adaptive_embedding([0.9, 0.1], path=store_path)

        vsid.reset_adaptive(path=store_path)
        store = vsid.load_store(store_path)
        assert store["speakers"]["alice"]["adaptive_embeddings"] == []
        assert store["speakers"]["alice"]["embeddings"] == [[1.0, 0.0]]

    def test_adaptive_samples_never_perturb_manual_consistency_stat(self, store_path):
        for embedding in ([1.0, 0.0], [0.99, 0.05], [0.98, -0.04]):
            vsid.enroll_embedding("Alice", embedding, path=store_path)
        baseline = vsid.list_speakers(path=store_path)[0]["consistency"]

        # A wildly inconsistent adaptive sample must not move the UI stat.
        vsid.append_adaptive_embedding([-1.0, 0.0], path=store_path)
        after = vsid.list_speakers(path=store_path)[0]["consistency"]
        assert after == baseline

    def test_list_speakers_reports_adaptive_count(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        vsid.append_adaptive_embedding([0.9, 0.1], path=store_path)
        vsid.append_adaptive_embedding([0.8, 0.2], path=store_path)

        speaker = vsid.list_speakers(path=store_path)[0]
        assert speaker["adaptive"] == 2

    def test_adaptive_embedding_extends_matching_via_max_sim(self, store_path, monkeypatch):
        """A manual enrollment orthogonal to the probe still matches once an
        adaptive sample closer to the probe is appended (max-sim, not
        average -- averaging the two would still miss)."""
        vsid.enroll_embedding("Alice", [0.0, 1.0], path=store_path)
        label, score = vsid.identify_embedding([1.0, 0.0], threshold=0.45, path=store_path)
        assert label == "unknown"  # orthogonal manual sample alone doesn't match

        vsid.append_adaptive_embedding([1.0, 0.0], path=store_path)
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: [1.0, 0.0])
        result = vsid.identify_zoned(_long_pcm(), cfg={}, path=store_path)
        assert result["zone"] == vsid.ZONE_OWNER
        assert result["score"] == pytest.approx(1.0)


class TestModelRegistry:
    def test_default_model_id_is_in_registry(self):
        assert vsid.DEFAULT_SPEAKER_MODEL_ID in vsid.SPEAKER_MODEL_REGISTRY

    def test_registry_has_a_stronger_alternative(self):
        assert len(vsid.SPEAKER_MODEL_REGISTRY) >= 2

    def test_resolve_model_id_defaults_when_unset(self):
        assert vsid.resolve_speaker_model_id({}) == vsid.DEFAULT_SPEAKER_MODEL_ID

    def test_resolve_model_id_honors_registry_selection(self):
        other = next(k for k in vsid.SPEAKER_MODEL_REGISTRY if k != vsid.DEFAULT_SPEAKER_MODEL_ID)
        cfg = {"voice": {"speaker_id": {"model": other}}}
        assert vsid.resolve_speaker_model_id(cfg) == other

    def test_resolve_model_id_synthesizes_id_for_local_path_override(self, tmp_path):
        local = tmp_path / "custom.onnx"
        cfg = {"voice": {"speaker_id": {"model": str(local)}}}
        assert vsid.resolve_speaker_model_id(cfg) == f"custom:{local}"

    def test_model_mismatch_false_on_empty_store(self, store_path):
        assert vsid.model_mismatch({}, path=store_path) is False

    def test_model_mismatch_false_when_model_id_matches(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path, model_id=vsid.DEFAULT_SPEAKER_MODEL_ID)
        assert vsid.model_mismatch({}, path=store_path) is False

    def test_model_mismatch_false_when_no_model_id_recorded(self, store_path):
        # Pre-registry store: no model_id key at all -- treated as the default.
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        assert vsid.model_mismatch({}, path=store_path) is False

    def test_model_mismatch_true_when_model_id_differs(self, store_path):
        other = next(k for k in vsid.SPEAKER_MODEL_REGISTRY if k != vsid.DEFAULT_SPEAKER_MODEL_ID)
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path, model_id=vsid.DEFAULT_SPEAKER_MODEL_ID)
        cfg = {"voice": {"speaker_id": {"model": other}}}
        assert vsid.model_mismatch(cfg, path=store_path) is True

    def test_list_speakers_surfaces_model_mismatch(self, store_path):
        other = next(k for k in vsid.SPEAKER_MODEL_REGISTRY if k != vsid.DEFAULT_SPEAKER_MODEL_ID)
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path, model_id=vsid.DEFAULT_SPEAKER_MODEL_ID)
        cfg = {"voice": {"speaker_id": {"model": other}}}

        speaker = vsid.list_speakers(cfg=cfg, path=store_path)[0]
        assert speaker["model_mismatch"] is True

    def test_enroll_stamps_resolved_model_id(self, store_path, monkeypatch):
        monkeypatch.setattr(vsid, "compute_embedding", lambda *a, **k: [1.0, 0.0])
        store = vsid.enroll("Alice", b"\x00\x00" * 100, cfg={}, path=store_path)
        assert store["model_id"] == vsid.DEFAULT_SPEAKER_MODEL_ID


class TestTtsSelfProfile:
    def test_no_profile_yet_scores_minus_one(self, store_path):
        assert vsid.tts_echo_score([1.0, 0.0], path=store_path) == -1.0

    def test_no_profile_yet_has_no_fingerprint(self, store_path):
        assert vsid.stored_tts_fingerprint(path=store_path) is None

    def test_store_and_match_tts_profile(self, store_path):
        vsid.store_tts_profile([[1.0, 0.0], [0.9, 0.1]], fingerprint="v1", path=store_path)

        assert vsid.stored_tts_fingerprint(path=store_path) == "v1"
        assert vsid.tts_echo_score([1.0, 0.0], path=store_path) == pytest.approx(1.0)

    def test_tts_profile_hidden_from_list_speakers(self, store_path):
        vsid.enroll_embedding("Alice", [1.0, 0.0], path=store_path)
        vsid.store_tts_profile([[0.0, 1.0]], fingerprint="v1", path=store_path)

        names = [s["name"] for s in vsid.list_speakers(path=store_path)]
        assert names == ["Alice"]

    def test_tts_profile_never_claims_owner_slot(self, store_path):
        # No human enrolled yet -- storing the TTS profile alone must not
        # make "__marvi_tts__" the owner.
        vsid.store_tts_profile([[1.0, 0.0]], fingerprint="v1", path=store_path)
        store = vsid.load_store(store_path)
        assert store["owner"] is None
