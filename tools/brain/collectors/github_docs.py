"""GitHub README/docs collector -- pulls documentation Markdown from the
user's own repositories into the Brain.

For each of the user's own (non-fork, non-org) repositories, capped at
``brain.collect.github_max_repos`` (most-recently-updated first), fetches
the top-level ``README.md`` and any Markdown files directly inside a
top-level ``docs/`` folder, and hands them to
``tools.brain.collected.write_collected_document()`` under source
``"github"``. A per ``repo+path`` cursor keyed to the file's git blob
``sha`` (``HERMES_HOME/brain/collectors/github.json``, via
``tools/brain/collectors/state.py``) means an unchanged file is a fast
no-op on the next pass -- only a real content change (new sha) re-collects
it.

Reuses the SAME Composio client seam the subconscious's read-only GitHub
notifications fetcher uses (``cron/scripts/subconscious/composio_client.py``)
-- this module only ever *imports* that seam, never modifies it (owned by a
parallel workstream).

Composio over the ``gh`` CLI: Marvi's account-integration story already
runs every connected surface (Gmail, GitHub, ...) through one Composio API
key, with auth/rate-limit/retry handling centralized in
``composio_client.py``. Reusing it here means zero new credential surface,
zero new subprocess dependency (``gh`` would need its own install + its own
``gh auth login`` outside Marvi's control, and wouldn't share the "clean
skip when unconfigured" story the email collector already has), and a
guarded-skip contract identical to the email collector when Composio isn't
connected.
"""

from __future__ import annotations

import base64
from typing import Any, Callable, Dict, List, Optional

from cron.scripts.subconscious.composio_client import (
    ComposioAuthError,
    ComposioUnavailable,
    get_api_key,
    get_client,
    unwrap_payload,
)
from tools.brain.collected import write_collected_document
from tools.brain.collectors.state import load_collector_state, save_collector_state

SOURCE = "github"
STATE_NAME = "github"

# Best-effort Composio GitHub toolkit action slugs -- kept as module
# constants (matching cron/scripts/subconscious/github.py's convention) so a
# future SDK/toolkit rename is a one-line fix.
ACTION_LIST_REPOS = "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER"
ACTION_GET_CONTENT = "GITHUB_GET_REPOSITORY_CONTENT"

DEFAULT_MAX_REPOS = 10
# Top-level README plus a top-level docs/ folder -- not recursive, bounded
# by MAX_DOC_FILES_PER_REPO, so a doc-heavy monorepo can't turn one repo
# into an unbounded fetch spree.
DOC_PATHS = ("README.md", "docs")
MAX_DOC_FILES_PER_REPO = 20


def _cursor_key(repo_full_name: str, path: str) -> str:
    return f"{repo_full_name}:{path}"


def _extract_repos(payload: Any) -> List[Dict[str, Any]]:
    body = unwrap_payload(payload)
    items = body.get("items") if isinstance(body, dict) else body
    if not isinstance(items, list):
        return []
    return [r for r in items if isinstance(r, dict)]


def _extract_content_entries(payload: Any) -> List[Dict[str, Any]]:
    """A get-content call returns either one file object or a directory
    listing (a list of file/dir objects) -- normalize both to a list."""
    body = unwrap_payload(payload)
    if isinstance(body, list):
        return [e for e in body if isinstance(e, dict)]
    if isinstance(body, dict):
        if isinstance(body.get("content"), str):
            return [body]
        items = body.get("items") or body.get("entries")
        if isinstance(items, list):
            return [e for e in items if isinstance(e, dict)]
    return []


def _decode_content(entry: Dict[str, Any]) -> str:
    raw = entry.get("content") or ""
    encoding = str(entry.get("encoding") or "base64")
    if encoding == "base64":
        try:
            return base64.b64decode(raw).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return str(raw)


def _is_own_repo(repo: Dict[str, Any]) -> bool:
    """"The user's own repos" -- excludes forks and anything owned by an
    org/other account the user merely has push access to."""
    if repo.get("fork"):
        return False
    owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
    owner_type = owner.get("type")
    return not owner_type or str(owner_type).lower() == "user"


def collect_github_documents(
    *,
    max_repos: int = DEFAULT_MAX_REPOS,
    client: Any = None,
    cursor_state: Optional[Dict[str, str]] = None,
    save_cursor_state: Optional[Callable[[Dict[str, str]], None]] = None,
) -> Dict[str, Any]:
    """Collect README/docs Markdown from the user's own repos into the
    Brain.

    ``client``/``cursor_state``/``save_cursor_state`` are injection points
    for tests (a fake client, an in-memory cursor dict) -- production
    callers leave them unset and get the real Composio client plus the
    on-disk cursor file. Returns a summary dict; a not-configured Composio
    is reported as ``{"ok": True, "skipped": "composio_not_configured", ...}``,
    never raised, so a Brain-indexer run with no GitHub connected is a
    no-op, not a failure.
    """
    if client is None:
        if not get_api_key():
            return {"ok": True, "skipped": "composio_not_configured", "collected": 0}
        try:
            client = get_client()
        except (ComposioAuthError, ComposioUnavailable) as exc:
            return {"ok": True, "skipped": f"composio_unavailable: {exc}", "collected": 0}

    if cursor_state is None or save_cursor_state is None:
        cursor_state = load_collector_state(STATE_NAME)
        save_cursor_state = lambda state: save_collector_state(STATE_NAME, state)  # noqa: E731

    state = dict(cursor_state)
    max_repos = max(0, int(max_repos))

    try:
        repos_payload = client.execute_action(ACTION_LIST_REPOS, {"per_page": max_repos or 1, "sort": "updated"})
    except Exception as exc:
        return {"ok": False, "error": str(exc), "collected": 0}

    repos = [r for r in _extract_repos(repos_payload) if _is_own_repo(r)][:max_repos]

    collected = skipped = errors = 0
    for repo in repos:
        full_name = repo.get("full_name")
        owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
        owner_login = owner.get("login")
        repo_name = repo.get("name")
        if not full_name or not owner_login or not repo_name:
            continue

        for doc_path in DOC_PATHS:
            try:
                content_payload = client.execute_action(
                    ACTION_GET_CONTENT, {"owner": owner_login, "repo": repo_name, "path": doc_path}
                )
            except Exception:
                errors += 1
                continue

            for entry in _extract_content_entries(content_payload)[:MAX_DOC_FILES_PER_REPO]:
                name = str(entry.get("name") or entry.get("path") or "")
                if not name.lower().endswith(".md"):
                    continue
                path = entry.get("path") or f"{doc_path}/{name}"
                sha = entry.get("sha")
                key = _cursor_key(full_name, path)
                if sha and state.get(key) == sha:
                    skipped += 1
                    continue
                text = _decode_content(entry)
                if not text.strip():
                    continue
                result = write_collected_document(
                    source=SOURCE, title=f"{full_name}: {path}", text=text, ref=f"{full_name}:{path}"
                )
                collected += 1 if result.get("written") else 0
                skipped += 0 if result.get("written") else 1
                if sha:
                    state[key] = sha

    save_cursor_state(state)
    return {"ok": True, "collected": collected, "skipped": skipped, "errors": errors, "repos_scanned": len(repos)}
