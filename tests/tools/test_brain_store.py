"""Tests for the local Brain document index — tools/brain/store.py (SQLite
FTS5) and tools/brain/indexer.py (chunking + incremental indexing).

Mirrors the conventions of tests/cron/test_deep_subconscious.py's
``test_brain_fts_searches_indexed_chunks`` smoke test, expanded to cover the
chunking function, store CRUD, incremental mtime+size skip, and deletion
purge called out in the 2026-07-14 Marvi deep-subconscious/brain hardening
pass.
"""

from __future__ import annotations

import pytest

from tools.brain.indexer import _chunks, _excluded


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


class TestChunking:
    def test_empty_text_yields_no_chunks(self):
        assert _chunks("") == []
        assert _chunks("   \n\n  ") == []

    def test_short_document_is_a_single_chunk(self):
        text = "Hello world, this is a short note."
        chunks = _chunks(text, size=1200, overlap=160)

        assert chunks == [text]

    def test_normalizes_crlf_and_strips_surrounding_whitespace(self):
        chunks = _chunks("\r\n  hello\r\nworld  \r\n", size=1200, overlap=160)

        assert chunks == ["hello\nworld"]

    def test_long_document_is_split_into_multiple_overlapping_chunks(self):
        text = "".join(f"{i:04d}" for i in range(1000))  # 4000 chars, deterministic content
        size, overlap = 1200, 160
        chunks = _chunks(text, size=size, overlap=overlap)

        assert len(chunks) > 1
        # Every chunk except (possibly) the last is exactly `size` chars.
        for chunk in chunks[:-1]:
            assert len(chunk) == size

        # Overlap: the tail of one chunk reappears at the head of the next.
        step = size - overlap
        for index in range(len(chunks) - 1):
            expected_overlap = text[(index + 1) * step : (index + 1) * step + overlap]
            assert chunks[index + 1][:overlap] == expected_overlap

    def test_chunk_sizes_respect_requested_size_and_overlap(self):
        text = "x" * 3000
        chunks = _chunks(text, size=1000, overlap=100)

        # Reconstructing the walk confirms the stride is size - overlap.
        stride = 1000 - 100
        assert len(chunks) == len(range(0, len(text), stride))


# ---------------------------------------------------------------------------
# Exclusion matching
# ---------------------------------------------------------------------------


class TestExcluded:
    def test_matches_path_component_by_exact_name(self, tmp_path):
        from pathlib import Path

        target = Path("/repo/node_modules/pkg/index.js")
        assert _excluded(target, [".git", "node_modules", "venv"]) is True

    def test_does_not_match_unrelated_path(self):
        from pathlib import Path

        target = Path("/repo/src/index.js")
        assert _excluded(target, [".git", "node_modules", "venv"]) is False

    def test_matches_glob_pattern(self):
        from pathlib import Path

        target = Path("/repo/build/output.min.js")
        assert _excluded(target, ["*build*"]) is True


# ---------------------------------------------------------------------------
# BrainStore CRUD + search
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    from tools.brain.store import BrainStore

    instance = BrainStore(tmp_path / "brain.db")
    yield instance
    instance.close()


class TestBrainStoreCrud:
    def test_replace_file_then_indexed_file_round_trips(self, store):
        store.replace_file("a.md", 100.0, 42, "2026-07-14T00:00:00+00:00", ["hello world"])

        row = store.indexed_file("a.md")

        assert row is not None
        assert row["mtime"] == 100.0
        assert row["size"] == 42

    def test_indexed_file_returns_none_when_absent(self, store):
        assert store.indexed_file("missing.md") is None

    def test_replace_file_overwrites_prior_chunks(self, store):
        store.replace_file("a.md", 1.0, 10, "t1", ["old chunk one", "old chunk two"])
        store.replace_file("a.md", 2.0, 20, "t2", ["new chunk"])

        results = store.search("new")
        assert len(results) == 1
        assert results[0]["path"] == "a.md"

        # The old chunks are gone — searching for their unique term finds nothing.
        assert store.search("old") == []

    def test_status_counts_files_and_chunks(self, store):
        store.replace_file("a.md", 1.0, 10, "2026-07-14T00:00:00+00:00", ["one", "two"])
        store.replace_file("b.md", 1.0, 10, "2026-07-14T01:00:00+00:00", ["three"])

        status = store.status()

        assert status["files"] == 2
        assert status["chunks"] == 3
        assert status["indexed_at"] == "2026-07-14T01:00:00+00:00"

    def test_status_on_empty_store(self, store):
        status = store.status()

        assert status["files"] == 0
        assert status["chunks"] == 0
        assert status["indexed_at"] is None


class TestBrainStoreSearchRelevance:
    def test_finds_matching_chunk_across_multiple_files(self, store):
        store.replace_file("voice.md", 1.0, 10, "t", ["Moonshine streaming voice notes"])
        store.replace_file("recipes.md", 1.0, 10, "t", ["Grandma's lasagna recipe"])

        results = store.search("streaming voice")

        assert len(results) == 1
        assert results[0]["path"] == "voice.md"

    def test_more_relevant_document_ranks_first(self, store):
        # bm25() is ascending (lower/more negative = more relevant in SQLite's
        # convention) and store.search ORDERs BY score ascending, so the chunk
        # that matches the query term more densely should surface first.
        store.replace_file(
            "dense.md", 1.0, 10, "t",
            ["contract contract contract renewal terms and contract details"],
        )
        store.replace_file(
            "sparse.md", 1.0, 10, "t",
            ["a long document about many unrelated topics that mentions contract once"],
        )

        results = store.search("contract")

        assert [r["path"] for r in results][0] == "dense.md"

    def test_no_terms_returns_empty(self, store):
        store.replace_file("a.md", 1.0, 10, "t", ["hello world"])

        assert store.search("") == []
        assert store.search("   ") == []

    def test_search_respects_limit(self, store):
        for i in range(10):
            store.replace_file(f"f{i}.md", 1.0, 10, "t", [f"shared keyword file {i}"])

        results = store.search("shared keyword", limit=3)

        assert len(results) == 3

    def test_snippet_is_included(self, store):
        store.replace_file("a.md", 1.0, 10, "t", ["The quick brown fox jumps over the lazy dog"])

        results = store.search("fox")

        assert "snippet" in results[0]
        assert "[" in results[0]["snippet"] and "]" in results[0]["snippet"]


class TestBrainStoreDeletionPurge:
    def test_remove_missing_deletes_files_not_in_live_set(self, store):
        store.replace_file("keep.md", 1.0, 10, "t", ["keep me"])
        store.replace_file("gone.md", 1.0, 10, "t", ["delete me"])

        removed = store.remove_missing({"keep.md"})

        assert removed == 1
        assert store.indexed_file("keep.md") is not None
        assert store.indexed_file("gone.md") is None
        assert store.search("delete") == []
        assert store.search("keep") != []

    def test_remove_missing_purges_chunks_too(self, store):
        store.replace_file("gone.md", 1.0, 10, "t", ["unique-term-xyz"])

        store.remove_missing(set())

        assert store.search("unique-term-xyz") == []

    def test_remove_missing_is_noop_when_everything_present(self, store):
        store.replace_file("a.md", 1.0, 10, "t", ["hello"])

        removed = store.remove_missing({"a.md"})

        assert removed == 0
        assert store.indexed_file("a.md") is not None


# ---------------------------------------------------------------------------
# Incremental indexing — unchanged mtime+size is skipped
# ---------------------------------------------------------------------------


class TestIncrementalIndexing:
    """Relies on the global ``_hermetic_environment`` autouse fixture
    (tests/conftest.py), which points HERMES_HOME at a fresh per-test tempdir
    — index_configured_folders' internal ``BrainStore()`` (no explicit path)
    therefore reads/writes there without any manual monkeypatching."""

    def test_index_configured_folders_skips_unchanged_files(self, tmp_path, _isolate_hermes_home):
        from tools.brain import indexer

        watched = tmp_path / "watched"
        watched.mkdir()
        (watched / "note.txt").write_text("hello world, first pass", encoding="utf-8")

        cfg = {"brain": {"enabled": True, "folders": [str(watched)], "exclude": []}}

        first = indexer.index_configured_folders(cfg)
        assert first["indexed"] == 1
        assert first["skipped"] == 0

        # Second pass over the exact same file (same mtime + size) must skip,
        # not re-chunk/re-embed it.
        second = indexer.index_configured_folders(cfg)
        assert second["indexed"] == 0
        assert second["skipped"] == 1

    def test_index_configured_folders_reindexes_changed_file(self, tmp_path, _isolate_hermes_home):
        from tools.brain import indexer

        watched = tmp_path / "watched"
        watched.mkdir()
        target = watched / "note.txt"
        target.write_text("version one", encoding="utf-8")

        cfg = {"brain": {"enabled": True, "folders": [str(watched)], "exclude": []}}
        indexer.index_configured_folders(cfg)

        # A different size alone (independent of mtime filesystem resolution)
        # is enough to defeat the "unchanged" skip check.
        target.write_text("version two, now much longer than before", encoding="utf-8")

        result = indexer.index_configured_folders(cfg)
        assert result["indexed"] == 1
        assert result["skipped"] == 0

    def test_index_configured_folders_purges_deleted_files(self, tmp_path, _isolate_hermes_home):
        from tools.brain import indexer

        watched = tmp_path / "watched"
        watched.mkdir()
        target = watched / "note.txt"
        target.write_text("will be deleted", encoding="utf-8")

        cfg = {"brain": {"enabled": True, "folders": [str(watched)], "exclude": []}}
        indexer.index_configured_folders(cfg)

        target.unlink()

        result = indexer.index_configured_folders(cfg)
        assert result["removed"] == 1

    def test_index_configured_folders_skips_oversized_files(self, tmp_path, monkeypatch, _isolate_hermes_home):
        from tools.brain import indexer

        monkeypatch.setattr(indexer, "MAX_FILE_BYTES", 10)

        watched = tmp_path / "watched"
        watched.mkdir()
        (watched / "big.txt").write_text("this file is definitely bigger than 10 bytes", encoding="utf-8")

        cfg = {"brain": {"enabled": True, "folders": [str(watched)], "exclude": []}}
        result = indexer.index_configured_folders(cfg)

        assert result["indexed"] == 0
        assert result["skipped"] == 1

    def test_index_configured_folders_records_last_run(self, tmp_path, _isolate_hermes_home):
        from tools.brain import indexer

        watched = tmp_path / "watched"
        watched.mkdir()
        (watched / "note.txt").write_text("hello", encoding="utf-8")

        cfg = {"brain": {"enabled": True, "folders": [str(watched)], "exclude": []}}
        indexer.index_configured_folders(cfg)

        last_run = indexer.read_last_run()
        assert last_run["at"]
        assert last_run["indexed"] == 1

    def test_read_last_run_defaults_when_absent(self, _isolate_hermes_home):
        from tools.brain import indexer

        last_run = indexer.read_last_run()

        assert last_run["at"] is None
        assert last_run["indexed"] == 0
