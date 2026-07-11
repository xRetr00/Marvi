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
    def test_require_owner_for_escalation_defaults_true(self):
        assert vsid.require_owner_for_escalation({}) is True

    def test_require_owner_for_escalation_respects_config(self):
        cfg = {"voice": {"speaker_id": {"require_owner_for_escalation": False}}}
        assert vsid.require_owner_for_escalation(cfg) is False
