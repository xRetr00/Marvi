"""Episodic memory store — a time-indexed log of what actually happened.

Loop 1 of the "memory maturity" round (see
``docs/superpowers/specs/2026-07-17-marvi-memory-maturity-spec.md``). This
gives Marvi a queryable, structured event log distinct from the curated
semantic memory in ``tools/memory_tool.py`` (USER.md/MEMORY.md) — episodes
are DERIVED from signals the agent already produces (activity feed, session
ends, the presence distiller), not written by the model directly.

Storage mirrors ``tools/brain/store.py``'s dependency-free approach: SQLite
+ FTS5, no vectors, no external services. Lives at
``HERMES_HOME/memory/episodic.db`` (a sibling of ``HERMES_HOME/brain/``).

Every public function is thread-safe and NEVER raises to the caller —
episodic recording is a background nicety layered on top of real work
(cron ticks, session finalization, the presence distiller); a failure here
must never break any of that. Failures are logged at debug level and
swallowed.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# kind ∈ conversation|task|room|proactive|device|arrival|learning (spec §1.1)
VALID_KINDS = frozenset({
    "conversation", "task", "room", "proactive", "device", "arrival", "learning",
})
# actor ∈ marvi|user|world (spec §1.1)
VALID_ACTORS = frozenset({"marvi", "user", "world"})

_DB_FILENAME = "episodic.db"
# Serializes connect+operate blocks within this process. Each call opens its
# own short-lived connection (mirrors tools/brain/store.py's per-call usage
# pattern) rather than holding one long-lived connection, so a stale handle
# from a profile switch (HERMES_HOME override) never lingers.
_lock = threading.RLock()


def _db_path() -> Path:
    return get_hermes_home() / "memory" / _DB_FILENAME


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            kind TEXT NOT NULL,
            actor TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            ref TEXT,
            entities_json TEXT NOT NULL DEFAULT '[]',
            importance REAL NOT NULL DEFAULT 0.5,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(ts);
        CREATE INDEX IF NOT EXISTS idx_episodes_source_ref ON episodes(source, ref);
        CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
            episode_id UNINDEXED, title, summary, entities,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    return conn


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def episodic_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the ``memory.episodic`` config section with defaults filled in.

    Uses ``cfg_get`` with inline defaults rather than ``DEFAULT_CONFIG`` —
    these keys are not UI-edited in Loop 1 (the Timeline tab is read-only).
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = config if config is not None else load_config()
        return {
            "enabled": bool(cfg_get(cfg, "memory", "episodic", "enabled", default=True)),
            "retain_days": int(cfg_get(cfg, "memory", "episodic", "retain_days", default=400) or 400),
            "min_importance_for_prompt": float(
                cfg_get(cfg, "memory", "episodic", "min_importance_for_prompt", default=0.4) or 0.4
            ),
        }
    except Exception:
        logger.debug("episodic: config read failed, using defaults", exc_info=True)
        return {"enabled": True, "retain_days": 400, "min_importance_for_prompt": 0.4}


def _enabled() -> bool:
    return episodic_config()["enabled"]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def record_episode(
    kind: str,
    title: str,
    summary: str = "",
    *,
    actor: str = "marvi",
    source: str,
    ref: Optional[str] = None,
    entities: Optional[List[str]] = None,
    importance: float = 0.5,
    ts: Optional[str] = None,
) -> Optional[int]:
    """Record one episode. Returns the row id, or ``None`` on failure/skip.

    Idempotent by ``(source, ref)`` when ``ref`` is given: a second call
    with the same (source, ref) pair is a no-op that returns the existing
    row's id instead of inserting a duplicate. Never raises.
    """
    if not _enabled():
        return None
    try:
        kind = str(kind or "").strip()
        actor = str(actor or "marvi").strip()
        source = str(source or "").strip()
        title = str(title or "").strip()
        summary = str(summary or "")
        if kind not in VALID_KINDS:
            logger.debug("episodic: dropping episode with invalid kind %r", kind)
            return None
        if actor not in VALID_ACTORS:
            actor = "marvi"
        if not title or not source:
            logger.debug("episodic: dropping episode with missing title/source")
            return None
        try:
            importance_value = max(0.0, min(1.0, float(importance)))
        except (TypeError, ValueError):
            importance_value = 0.5
        ts_value = str(ts) if ts else datetime.now(timezone.utc).isoformat()
        entities_list = [str(e).strip() for e in (entities or []) if str(e).strip()]
        ref_value = str(ref) if ref else None

        with _lock:
            conn = _connect()
            try:
                if ref_value:
                    existing = conn.execute(
                        "SELECT id FROM episodes WHERE source = ? AND ref = ?",
                        (source, ref_value),
                    ).fetchone()
                    if existing is not None:
                        return int(existing["id"])
                with conn:
                    cur = conn.execute(
                        """INSERT INTO episodes
                           (ts, kind, actor, title, summary, source, ref, entities_json, importance, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            ts_value, kind, actor, title, summary, source, ref_value,
                            json.dumps(entities_list, ensure_ascii=False), importance_value,
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    episode_id = cur.lastrowid
                    conn.execute(
                        "INSERT INTO episodes_fts(episode_id, title, summary, entities) VALUES (?, ?, ?, ?)",
                        (episode_id, title, summary, " ".join(entities_list)),
                    )
                return int(episode_id)
            finally:
                conn.close()
    except Exception:
        logger.debug("episodic: record_episode failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    raw_entities = d.pop("entities_json", None)
    try:
        d["entities"] = json.loads(raw_entities or "[]")
    except (TypeError, ValueError):
        d["entities"] = []
    return d


def _fts_query_string(text: str) -> str:
    terms = re.findall(r"[\w-]+", text, flags=re.UNICODE)
    if not terms:
        return ""
    return " AND ".join(f'"{t}"' for t in terms[:12])


def _entity_clause(entities: Optional[List[str]], column: str) -> tuple[str, List[Any]]:
    if not entities:
        return "", []
    clauses = []
    params: List[Any] = []
    for entity in entities:
        entity = str(entity).strip()
        if not entity:
            continue
        clauses.append(f"{column} LIKE ?")
        params.append(f"%{entity}%")
    if not clauses:
        return "", []
    return " AND (" + " AND ".join(clauses) + ")", params


def _query_fts(
    conn: sqlite3.Connection, text: str, kind: Optional[str], since: Optional[str],
    until: Optional[str], entities: Optional[List[str]], limit: int,
) -> List[Dict[str, Any]]:
    fts_q = _fts_query_string(text)
    if not fts_q:
        return []
    sql = (
        "SELECT e.*, bm25(episodes_fts) AS score FROM episodes_fts "
        "JOIN episodes e ON e.id = episodes_fts.episode_id "
        "WHERE episodes_fts MATCH ?"
    )
    params: List[Any] = [fts_q]
    if kind:
        sql += " AND e.kind = ?"
        params.append(kind)
    if since:
        sql += " AND e.ts >= ?"
        params.append(since)
    if until:
        sql += " AND e.ts <= ?"
        params.append(until)
    clause, entity_params = _entity_clause(entities, "e.entities_json")
    sql += clause
    params.extend(entity_params)
    sql += " ORDER BY score, e.importance DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def _query_filtered(
    conn: sqlite3.Connection, kind: Optional[str], since: Optional[str],
    until: Optional[str], entities: Optional[List[str]], limit: int,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM episodes WHERE 1=1"
    params: List[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if since:
        sql += " AND ts >= ?"
        params.append(since)
    if until:
        sql += " AND ts <= ?"
        params.append(until)
    clause, entity_params = _entity_clause(entities, "entities_json")
    sql += clause
    params.extend(entity_params)
    sql += " ORDER BY ts DESC, importance DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def query(
    *,
    text: Optional[str] = None,
    kind: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    entities: Optional[List[str]] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Query episodes, newest-first (importance as tiebreaker).

    Uses FTS5 when ``text`` is given, otherwise a plain time/kind filter.
    Never raises — returns ``[]`` on any failure.
    """
    try:
        limit = max(1, min(int(limit or 20), 200))
    except (TypeError, ValueError):
        limit = 20
    kind = str(kind).strip() if kind else None
    if kind is not None and kind not in VALID_KINDS:
        logger.debug("episodic: query ignoring invalid kind %r", kind)
        kind = None
    try:
        with _lock:
            conn = _connect()
            try:
                if text and str(text).strip():
                    return _query_fts(conn, str(text).strip(), kind, since, until, entities, limit)
                return _query_filtered(conn, kind, since, until, entities, limit)
            finally:
                conn.close()
    except Exception:
        logger.debug("episodic: query failed", exc_info=True)
        return []


def recent(limit: int = 20) -> List[Dict[str, Any]]:
    """Return the most recent episodes, newest first."""
    return query(limit=limit)


def count() -> int:
    """Return the total number of stored episodes. Returns 0 on failure."""
    try:
        with _lock:
            conn = _connect()
            try:
                row = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
    except Exception:
        logger.debug("episodic: count failed", exc_info=True)
        return 0


def purge_before(ts: str) -> int:
    """Delete every episode older than ``ts`` (exclusive of ``ts`` itself).

    Returns the number of rows deleted (0 on failure or no matches). Used
    by the retention/decay pass (Loop 3) — Loop 1 exposes the primitive
    without wiring an automatic cron trigger.
    """
    if not ts:
        return 0
    try:
        with _lock:
            conn = _connect()
            try:
                with conn:
                    ids = [row["id"] for row in conn.execute("SELECT id FROM episodes WHERE ts < ?", (str(ts),))]
                    if not ids:
                        return 0
                    conn.executemany("DELETE FROM episodes_fts WHERE episode_id = ?", [(i,) for i in ids])
                    conn.execute("DELETE FROM episodes WHERE ts < ?", (str(ts),))
                return len(ids)
            finally:
                conn.close()
    except Exception:
        logger.debug("episodic: purge_before failed", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Formatting (shared by the recall tool and the reflection prompt block)
# ---------------------------------------------------------------------------


def format_episode(ep: Dict[str, Any]) -> str:
    """Render one episode as a compact single line: time, kind, title, summary."""
    ts = ep.get("ts") or ""
    kind = ep.get("kind") or ""
    actor = ep.get("actor") or ""
    title = ep.get("title") or ""
    summary = str(ep.get("summary") or "").strip()
    line = f"- [{ts}] ({kind}/{actor}) {title}"
    if summary:
        snippet = summary if len(summary) <= 200 else summary[:197] + "..."
        line += f" — {snippet}"
    return line


# ---------------------------------------------------------------------------
# Session-end ingestion
#
# Registers as a listener on the EXISTING ``on_session_finalize`` plugin
# hook (hermes_cli/plugins.py, VALID_HOOKS) instead of editing gateway/run.py
# (outside this module's ownership for Loop 1) — the gateway's session
# expiry watcher already calls ``invoke_hook("on_session_finalize", ...)``
# exactly once per session when it's finalized (expiry or /reset), so this
# listener fires once per session without touching gateway code at all.
# Each callback registered on a hook is individually try/except-wrapped by
# PluginManager.invoke_hook, so a failure here can never break session
# finalization. CLI-only sessions (no gateway) are NOT covered by this path
# — see the Loop 1 final report for that documented gap.
# ---------------------------------------------------------------------------

_session_finalize_hook_registered = False
_session_finalize_hook_lock = threading.Lock()


def _cheap_session_summary(session_id: str) -> tuple[str, str]:
    """Best-effort (title, summary) for a session — NO LLM call.

    Reuses the session's already-computed ``title``/``preview`` columns
    (see ``hermes_state.SessionDB``). Returns ``("", "")`` when the session
    can't be found or has no title — callers treat an empty title as
    "nothing worth recording".
    """
    try:
        from hermes_state import SessionDB, DEFAULT_DB_PATH

        db = SessionDB(db_path=DEFAULT_DB_PATH, read_only=True)
        row = db.get_session(session_id)
    except Exception:
        row = None
    if not row:
        return "", ""
    title = str(row.get("title") or "").strip()
    preview = str(row.get("preview") or "").strip()
    if not title:
        return "", ""
    return title, preview


def _on_session_finalize(*, session_id: str = "", platform: str = "", reason: str = "", **_: Any) -> None:
    """Record one 'conversation' episode when a session is finalized."""
    if not session_id:
        return
    try:
        title, summary = _cheap_session_summary(session_id)
        if not title:
            return
        record_episode(
            kind="conversation",
            title=title,
            summary=summary,
            actor="user",
            source="session",
            ref=session_id,
        )
    except Exception:
        logger.debug("episodic: session-finalize episode failed", exc_info=True)


def register_session_finalize_hook() -> None:
    """Idempotently register :func:`_on_session_finalize` on the plugin
    manager's ``on_session_finalize`` hook. Safe to call repeatedly; safe
    to fail (e.g. under a stripped-down test harness with no plugins
    module importable) — never raises.
    """
    global _session_finalize_hook_registered
    if _session_finalize_hook_registered:
        return
    with _session_finalize_hook_lock:
        if _session_finalize_hook_registered:
            return
        try:
            from hermes_cli.plugins import get_plugin_manager

            manager = get_plugin_manager()
            hooks = getattr(manager, "_hooks", None)
            if isinstance(hooks, dict):
                callbacks = hooks.setdefault("on_session_finalize", [])
                if _on_session_finalize not in callbacks:
                    callbacks.append(_on_session_finalize)
                _session_finalize_hook_registered = True
        except Exception:
            logger.debug("episodic: could not register on_session_finalize hook", exc_info=True)


# Best-effort, side-effect-free-on-failure registration at import time so any
# process that imports this module (tool discovery, cron, the gateway) picks
# up session-end recording without needing an explicit call site.
register_session_finalize_hook()
