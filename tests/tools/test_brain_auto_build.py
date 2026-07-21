"""Tests for the Brain "self-feeding" auto-build pass added 2026-07-20:

* ``tools/brain/discovery.py`` -- PC folder auto-discovery (ranking, the
  auto-add cap, never touching the manually-configured folder list, and
  code-heavy-folder exclusion).
* ``tools/brain/collected.py`` + ``tools/brain_ingest_tool.py`` --
  ``brain_store_document``'s dedup-by-(source,ref)-or-content-hash and
  immediate-index behavior, plus tool registration.
* ``tools/brain/collectors/email_docs.py`` and
  ``tools/brain/collectors/github_docs.py`` -- cursor/state handling,
  attachment/doc filtering, sha-skip, repo cap, and the guarded clean-skip
  when Composio isn't configured (all against fake clients -- no network,
  no real Composio/gh dependency).
* ``tools/brain/indexer.py::brain_status`` -- the additive status fields
  (discovered_folders, last_discovery, collected, last_collect) consumed by
  ``GET /api/brain/status`` and the Brain tab.

Mirrors tests/tools/test_brain_store.py's conventions: the autouse
``_hermetic_environment`` fixture (tests/conftest.py) points HERMES_HOME at
a fresh per-test tempdir, so every on-disk helper here (collected/,
collectors/, discovery_last_run.json, ...) reads/writes there without any
manual monkeypatching.
"""

from __future__ import annotations

import base64
import json

import pytest


# ---------------------------------------------------------------------------
# discover_document_folders -- pure ranking function
# ---------------------------------------------------------------------------


class TestDiscoverDocumentFolders:
    def _make_home(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        return home

    def test_ranks_folders_by_document_density(self, tmp_path, _isolate_hermes_home):
        from tools.brain.discovery import discover_document_folders

        home = self._make_home(tmp_path)
        docs = home / "Documents"
        docs.mkdir()
        (docs / "a.txt").write_text("one", encoding="utf-8")
        (docs / "b.md").write_text("two", encoding="utf-8")
        (docs / "c.md").write_text("three", encoding="utf-8")

        desktop = home / "Desktop"
        desktop.mkdir()
        (desktop / "only.txt").write_text("one", encoding="utf-8")

        results = discover_document_folders(home=home, max_folders=5)

        paths = [r["path"] for r in results]
        assert str(docs.resolve()) in paths
        assert str(desktop.resolve()) in paths
        # Documents (3 docs) ranks above Desktop (1 doc).
        assert paths.index(str(docs.resolve())) < paths.index(str(desktop.resolve()))

    def test_respects_max_folders_cap(self, tmp_path, _isolate_hermes_home):
        from tools.brain.discovery import discover_document_folders

        home = self._make_home(tmp_path)
        docs = home / "Documents"
        docs.mkdir()
        for i in range(6):
            sub = docs / f"topic-{i}"
            sub.mkdir()
            (sub / "note.txt").write_text("x" * (i + 1), encoding="utf-8")

        results = discover_document_folders(home=home, max_folders=3)

        assert len(results) == 3

    def test_never_returns_a_folder_already_in_already_used(self, tmp_path, _isolate_hermes_home):
        from tools.brain.discovery import discover_document_folders

        home = self._make_home(tmp_path)
        docs = home / "Documents"
        docs.mkdir()
        (docs / "note.txt").write_text("hello", encoding="utf-8")

        results = discover_document_folders(home=home, already_used=[str(docs)], max_folders=5)

        assert results == []

    def test_excludes_code_heavy_folders(self, tmp_path, _isolate_hermes_home):
        from tools.brain.discovery import discover_document_folders

        home = self._make_home(tmp_path)
        docs = home / "Documents"
        docs.mkdir()
        repo = docs / "my-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "README.md").write_text("readme content", encoding="utf-8")
        (repo / "notes.txt").write_text("more content", encoding="utf-8")

        results = discover_document_folders(home=home, max_folders=5)

        paths = [r["path"] for r in results]
        assert str(repo.resolve()) not in paths

    def test_excludes_folders_matching_exclude_patterns(self, tmp_path, _isolate_hermes_home):
        from tools.brain.discovery import discover_document_folders

        home = self._make_home(tmp_path)
        docs = home / "Documents"
        docs.mkdir()
        node_modules = docs / "node_modules"
        node_modules.mkdir()
        (node_modules / "pkg.md").write_text("noise", encoding="utf-8")

        results = discover_document_folders(home=home, exclude=["node_modules"], max_folders=5)

        paths = [r["path"] for r in results]
        assert str(node_modules.resolve()) not in paths

    def test_ignores_folders_with_no_indexable_files(self, tmp_path, _isolate_hermes_home):
        from tools.brain.discovery import discover_document_folders

        home = self._make_home(tmp_path)
        docs = home / "Documents"
        docs.mkdir()
        empty = docs / "empty-folder"
        empty.mkdir()
        (empty / "image.png").write_bytes(b"\x89PNG")

        results = discover_document_folders(home=home, max_folders=5)

        paths = [r["path"] for r in results]
        assert str(empty.resolve()) not in paths

    def test_missing_candidate_roots_yield_no_candidates(self, tmp_path, _isolate_hermes_home):
        from tools.brain.discovery import discover_document_folders

        home = self._make_home(tmp_path)  # no Documents/Desktop/Downloads at all

        assert discover_document_folders(home=home, max_folders=5) == []


# ---------------------------------------------------------------------------
# run_discovery -- throttled orchestration + config mutation contract
# ---------------------------------------------------------------------------


class TestRunDiscovery:
    def test_populates_auto_folders_without_touching_manual_folders(self, monkeypatch, _isolate_hermes_home):
        from tools.brain import discovery

        monkeypatch.setattr(
            discovery,
            "discover_document_folders",
            lambda **_: [{"path": "D:\\Discovered", "count": 7}],
        )

        cfg = {"brain": {"folders": ["D:\\Manual"], "auto_discover": True}}
        result = discovery.run_discovery(cfg)

        assert result["ran"] is True
        assert cfg["brain"]["auto_folders"] == ["D:\\Discovered"]
        # The manually-configured list is read, never rewritten.
        assert cfg["brain"]["folders"] == ["D:\\Manual"]

    def test_second_call_within_24h_is_throttled(self, monkeypatch, _isolate_hermes_home):
        from tools.brain import discovery

        monkeypatch.setattr(discovery, "discover_document_folders", lambda **_: [{"path": "D:\\X", "count": 1}])

        cfg = {"brain": {"folders": [], "auto_discover": True}}
        first = discovery.run_discovery(cfg)
        second = discovery.run_discovery(cfg)

        assert first["ran"] is True
        assert second["ran"] is False
        assert second["reason"] == "throttled"

    def test_force_bypasses_the_throttle(self, monkeypatch, _isolate_hermes_home):
        from tools.brain import discovery

        monkeypatch.setattr(discovery, "discover_document_folders", lambda **_: [{"path": "D:\\X", "count": 1}])

        cfg = {"brain": {"folders": [], "auto_discover": True}}
        discovery.run_discovery(cfg)
        forced = discovery.run_discovery(cfg, force=True)

        assert forced["ran"] is True

    def test_disabled_auto_discover_skips_without_scanning(self, monkeypatch, _isolate_hermes_home):
        from tools.brain import discovery

        called = []
        monkeypatch.setattr(
            discovery, "discover_document_folders", lambda **kw: called.append(kw) or []
        )

        cfg = {"brain": {"folders": [], "auto_discover": False}}
        result = discovery.run_discovery(cfg)

        assert result["ran"] is False
        assert result["reason"] == "disabled"
        assert called == []

    def test_respects_max_auto_folders_from_config(self, monkeypatch, _isolate_hermes_home):
        from tools.brain import discovery

        captured = {}

        def fake_discover(**kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(discovery, "discover_document_folders", fake_discover)

        cfg = {"brain": {"folders": [], "auto_discover": True, "max_auto_folders": 2}}
        discovery.run_discovery(cfg)

        assert captured["max_folders"] == 2


# ---------------------------------------------------------------------------
# write_collected_document -- dedup + immediate index
# ---------------------------------------------------------------------------


class TestWriteCollectedDocument:
    def test_writes_a_markdown_file_under_collected_source_slug(self, _isolate_hermes_home):
        from tools.brain.collected import write_collected_document

        result = write_collected_document(source="chat", title="Vacation Policy", text="Employees get 20 days off.")

        assert result["ok"] is True
        assert result["written"] is True
        assert "collected" in result["path"].replace("\\", "/")
        assert "chat" in result["path"].replace("\\", "/")

    def test_repeated_save_with_same_ref_and_unchanged_text_is_skipped(self, _isolate_hermes_home):
        from tools.brain.collected import write_collected_document

        first = write_collected_document(source="chat", title="Policy", text="Same content.", ref="policy-1")
        second = write_collected_document(source="chat", title="Policy", text="Same content.", ref="policy-1")

        assert first["written"] is True
        assert second["written"] is False
        assert second["skipped"] is True

    def test_same_ref_with_changed_text_is_rewritten(self, _isolate_hermes_home):
        from tools.brain.collected import write_collected_document

        write_collected_document(source="chat", title="Policy", text="Version one.", ref="policy-1")
        second = write_collected_document(source="chat", title="Policy", text="Version two, much longer now.", ref="policy-1")

        assert second["written"] is True

    def test_dedup_by_content_hash_when_no_ref_given(self, _isolate_hermes_home):
        from tools.brain.collected import write_collected_document

        first = write_collected_document(source="chat", title="Note A", text="Identical body text.")
        second = write_collected_document(source="chat", title="Note B", text="Identical body text.")

        # Same content hash, no ref on either -- second is a dedup skip even
        # though the title differs.
        assert first["written"] is True
        assert second["skipped"] is True

    def test_immediately_indexes_the_written_document(self, _isolate_hermes_home):
        from tools.brain.collected import write_collected_document
        from tools.brain.store import BrainStore

        write_collected_document(source="chat", title="Searchable Doc", text="unique-marker-zzyzx appears here")

        store = BrainStore()
        try:
            results = store.search("unique-marker-zzyzx")
        finally:
            store.close()
        assert len(results) == 1

    def test_collected_counts_reports_per_source_totals(self, _isolate_hermes_home):
        from tools.brain.collected import collected_counts, write_collected_document

        write_collected_document(source="chat", title="One", text="alpha content here")
        write_collected_document(source="chat", title="Two", text="beta content here")
        write_collected_document(source="github", title="Three", text="gamma content here")

        counts = collected_counts()

        assert counts["chat"] == 2
        assert counts["github"] == 1


# ---------------------------------------------------------------------------
# brain_store_document tool -- registration + end-to-end dispatch
# ---------------------------------------------------------------------------


class TestBrainStoreDocumentTool:
    def test_registers_under_the_memory_toolset_with_a_schema(self, _isolate_hermes_home):
        import tools.brain_ingest_tool  # noqa: F401 -- self-registers on import
        from tools.registry import registry

        assert "brain_store_document" in registry.get_all_tool_names()
        schema = registry.get_schema("brain_store_document")
        assert schema is not None
        assert schema["name"] == "brain_store_document"
        for field in ("title", "text", "source"):
            assert field in schema["parameters"]["properties"]
        assert registry.get_toolset_for_tool("brain_store_document") == "memory"

    def test_dispatch_stores_and_returns_a_json_string(self, _isolate_hermes_home):
        import tools.brain_ingest_tool  # noqa: F401
        from tools.registry import registry

        raw = registry.dispatch(
            "brain_store_document",
            {"title": "Dispatch Test", "text": "content via dispatch", "source": "chat"},
        )

        assert isinstance(raw, str)
        payload = json.loads(raw)
        assert payload["success"] is True
        assert payload["written"] is True

    def test_dispatch_rejects_missing_required_fields(self, _isolate_hermes_home):
        import tools.brain_ingest_tool  # noqa: F401
        from tools.registry import registry

        raw = registry.dispatch("brain_store_document", {"title": "", "text": "x", "source": "chat"})

        payload = json.loads(raw)
        assert "error" in payload


# ---------------------------------------------------------------------------
# Email collector -- cursor/state, attachment filter, guarded skip
# ---------------------------------------------------------------------------


class _FakeGmailClient:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def execute_action(self, action, params=None):
        self.calls.append((action, params))
        response = self._responses.get(action)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(params)
        return response


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


class TestEmailCollector:
    def test_guarded_skip_when_composio_not_configured(self, monkeypatch, _isolate_hermes_home):
        from tools.brain.collectors import email_docs

        monkeypatch.setattr(email_docs, "get_api_key", lambda: None)

        result = email_docs.collect_email_documents()

        assert result["ok"] is True
        assert result["skipped"] == "composio_not_configured"

    def test_first_run_establishes_baseline_without_collecting(self, _isolate_hermes_home):
        from tools.brain.collectors import email_docs

        client = _FakeGmailClient({})
        result = email_docs.collect_email_documents(client=client, cursor_state={}, save_cursor_state=lambda s: None)

        assert result["first_run"] is True
        assert result["collected"] == 0
        assert client.calls == []  # never even lists messages on first run

    def test_collects_a_long_body_and_a_doc_attachment_skips_a_disallowed_extension(self, _isolate_hermes_home):
        from tools.brain.collectors import email_docs

        long_body = "x" * 2500
        message = {
            "payload": {
                "headers": [{"name": "Subject", "value": "Quarterly Report"}, {"name": "From", "value": "a@b.com"}],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64(long_body.encode("utf-8"))}},
                ],
            }
        }
        message["payload"]["parts"].append(
            {"filename": "notes.txt", "mimeType": "text/plain", "body": {"attachmentId": "att1", "size": 10}}
        )
        message["payload"]["parts"].append(
            {"filename": "virus.exe", "mimeType": "application/octet-stream", "body": {"attachmentId": "att2", "size": 10}}
        )

        responses = {
            email_docs.ACTION_LIST_MESSAGES: {"messages": [{"id": "m1"}]},
            email_docs.ACTION_GET_MESSAGE: message,
            email_docs.ACTION_GET_ATTACHMENT: {"data": _b64(b"attachment file contents")},
        }
        client = _FakeGmailClient(responses)
        saved = {}
        result = email_docs.collect_email_documents(
            client=client,
            cursor_state={"since": "2026-07-01T00:00:00+00:00"},
            save_cursor_state=lambda s: saved.update(s),
        )

        assert result["ok"] is True
        # One long body + one allowed (.txt) attachment collected; the .exe
        # attachment is filtered out before any attachment fetch call for it.
        assert result["collected"] == 2
        attachment_calls = [c for c in client.calls if c[0] == email_docs.ACTION_GET_ATTACHMENT]
        assert len(attachment_calls) == 1
        assert "m1" in saved.get("seen_ids", [])

    def test_oversized_attachment_is_skipped(self, monkeypatch, _isolate_hermes_home):
        from tools.brain.collectors import email_docs

        monkeypatch.setattr(email_docs, "MAX_ATTACHMENT_BYTES", 5)
        message = {
            "payload": {
                "headers": [{"name": "Subject", "value": "S"}, {"name": "From", "value": "a@b.com"}],
                "parts": [
                    {"filename": "big.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "att1", "size": 999}},
                ],
            }
        }
        responses = {
            email_docs.ACTION_LIST_MESSAGES: {"messages": [{"id": "m1"}]},
            email_docs.ACTION_GET_MESSAGE: message,
        }
        client = _FakeGmailClient(responses)

        result = email_docs.collect_email_documents(
            client=client, cursor_state={"since": "2026-07-01T00:00:00+00:00"}, save_cursor_state=lambda s: None
        )

        assert result["collected"] == 0
        assert not any(c[0] == email_docs.ACTION_GET_ATTACHMENT for c in client.calls)

    def test_already_seen_message_is_not_refetched(self, _isolate_hermes_home):
        from tools.brain.collectors import email_docs

        client = _FakeGmailClient({email_docs.ACTION_LIST_MESSAGES: {"messages": [{"id": "m1"}]}})

        result = email_docs.collect_email_documents(
            client=client,
            cursor_state={"since": "2026-07-01T00:00:00+00:00", "seen_ids": ["m1"]},
            save_cursor_state=lambda s: None,
        )

        assert result["collected"] == 0
        assert not any(c[0] == email_docs.ACTION_GET_MESSAGE for c in client.calls)


# ---------------------------------------------------------------------------
# GitHub collector -- sha-skip, repo cap, guarded skip
# ---------------------------------------------------------------------------


class _FakeGithubClient:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def execute_action(self, action, params=None):
        self.calls.append((action, params))
        response = self._responses.get(action)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(params)
        return response


def _repo(name, owner_login="me", owner_type="User", fork=False):
    return {"name": name, "full_name": f"{owner_login}/{name}", "fork": fork, "owner": {"login": owner_login, "type": owner_type}}


def _readme_entry(content: str, sha: str):
    return {"name": "README.md", "path": "README.md", "sha": sha, "encoding": "base64", "content": base64.b64encode(content.encode("utf-8")).decode("ascii")}


class TestGithubCollector:
    def test_guarded_skip_when_composio_not_configured(self, monkeypatch, _isolate_hermes_home):
        from tools.brain.collectors import github_docs

        monkeypatch.setattr(github_docs, "get_api_key", lambda: None)

        result = github_docs.collect_github_documents()

        assert result["ok"] is True
        assert result["skipped"] == "composio_not_configured"

    def test_collects_readme_from_owned_repos(self, _isolate_hermes_home):
        from tools.brain.collectors import github_docs

        def content_response(params):
            if params["path"] == "README.md":
                return _readme_entry("hello world readme", "sha-1")
            return []  # empty docs/ folder

        responses = {
            github_docs.ACTION_LIST_REPOS: {"items": [_repo("proj")]},
            github_docs.ACTION_GET_CONTENT: content_response,
        }
        client = _FakeGithubClient(responses)

        result = github_docs.collect_github_documents(
            client=client, cursor_state={}, save_cursor_state=lambda s: None
        )

        assert result["ok"] is True
        assert result["collected"] == 1

    def test_unchanged_sha_is_skipped_on_the_next_pass(self, _isolate_hermes_home):
        from tools.brain.collectors import github_docs

        def content_response(params):
            if params["path"] == "README.md":
                return _readme_entry("hello world readme", "sha-1")
            return []

        responses = {
            github_docs.ACTION_LIST_REPOS: {"items": [_repo("proj")]},
            github_docs.ACTION_GET_CONTENT: content_response,
        }
        client = _FakeGithubClient(responses)
        state = {}

        def save(s):
            state.update(s)

        first = github_docs.collect_github_documents(client=client, cursor_state=state, save_cursor_state=save)
        second = github_docs.collect_github_documents(client=client, cursor_state=state, save_cursor_state=save)

        assert first["collected"] == 1
        assert second["collected"] == 0
        assert second["skipped"] >= 1

    def test_changed_sha_is_recollected(self, _isolate_hermes_home):
        from tools.brain.collectors import github_docs

        call_count = {"n": 0}

        def content_response(params):
            if params["path"] != "README.md":
                return []
            call_count["n"] += 1
            sha = "sha-1" if call_count["n"] == 1 else "sha-2"
            text = "version one" if sha == "sha-1" else "version two, updated"
            return _readme_entry(text, sha)

        responses = {
            github_docs.ACTION_LIST_REPOS: {"items": [_repo("proj")]},
            github_docs.ACTION_GET_CONTENT: content_response,
        }
        client = _FakeGithubClient(responses)
        state = {}

        def save(s):
            state.update(s)

        first = github_docs.collect_github_documents(client=client, cursor_state=state, save_cursor_state=save)
        second = github_docs.collect_github_documents(client=client, cursor_state=state, save_cursor_state=save)

        assert first["collected"] == 1
        assert second["collected"] == 1

    def test_respects_max_repos_cap(self, _isolate_hermes_home):
        from tools.brain.collectors import github_docs

        repos = [_repo(f"proj-{i}") for i in range(5)]
        responses = {
            github_docs.ACTION_LIST_REPOS: {"items": repos},
            github_docs.ACTION_GET_CONTENT: lambda params: [],
        }
        client = _FakeGithubClient(responses)

        result = github_docs.collect_github_documents(
            client=client, max_repos=2, cursor_state={}, save_cursor_state=lambda s: None
        )

        assert result["repos_scanned"] == 2

    def test_forks_and_non_user_owned_repos_are_excluded(self, _isolate_hermes_home):
        from tools.brain.collectors import github_docs

        repos = [
            _repo("mine"),
            _repo("a-fork", fork=True),
            _repo("org-repo", owner_login="some-org", owner_type="Organization"),
        ]
        responses = {
            github_docs.ACTION_LIST_REPOS: {"items": repos},
            github_docs.ACTION_GET_CONTENT: lambda params: [],
        }
        client = _FakeGithubClient(responses)

        result = github_docs.collect_github_documents(
            client=client, max_repos=10, cursor_state={}, save_cursor_state=lambda s: None
        )

        assert result["repos_scanned"] == 1


# ---------------------------------------------------------------------------
# brain_status -- additive status fields
# ---------------------------------------------------------------------------


class TestBrainStatusAdditive:
    def test_status_includes_self_feeding_fields(self, _isolate_hermes_home):
        from tools.brain.indexer import brain_status

        status = brain_status({"brain": {"auto_discover": False, "max_auto_folders": 3}})

        for key in (
            "auto_discover",
            "max_auto_folders",
            "auto_folders",
            "collect_email",
            "collect_github",
            "github_max_repos",
            "discovered_folders",
            "last_discovery",
            "collected",
            "last_collect",
        ):
            assert key in status
        assert status["auto_discover"] is False
        assert status["max_auto_folders"] == 3
        # Original fields are still present -- purely additive.
        assert "enabled" in status
        assert "files" in status
        assert "last_run" in status

    def test_last_discovery_defaults_when_never_run(self, _isolate_hermes_home):
        from tools.brain.indexer import brain_status

        status = brain_status({"brain": {}})

        assert status["last_discovery"]["at"] is None
        assert status["discovered_folders"] == []

    def test_collected_counts_reflect_written_documents(self, _isolate_hermes_home):
        from tools.brain.collected import write_collected_document
        from tools.brain.indexer import brain_status

        write_collected_document(source="github", title="Readme", text="repo readme content")

        status = brain_status({"brain": {}})

        assert status["collected"].get("github") == 1
