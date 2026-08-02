"""Graph memory store — Marvi's knowledge graph (Part 2 of the "Marvi freedom
and graph mind" spec, see
``docs/superpowers/specs/2026-07-20-marvi-freedom-and-graph-mind-spec.md``,
§2.2).

The flat `§`-delimited semantic memory (``tools/memory_tool.py``) and the
episodic log (``agent/memory/episodic.py``) hold Marvi's knowledge as
disconnected rows. This module is an ADDITIVE layer over them: nodes
(people, projects, facts, events, preferences, ...) with typed edges
(works_on, related_to, contradicts, ...) that reference their source entry
(a memory hash / episode id / brain doc) rather than duplicating it. The
`§` files and episodic.db stay the durable source of truth for their
content — this module indexes and RELATES them.

Storage mirrors ``agent/memory/episodic.py``'s dependency-free approach:
SQLite + FTS5, no vectors, no external graph database. Lives at
``HERMES_HOME/memory/graph.db`` (a sibling of ``episodic.db``).

Every public function is thread-safe and NEVER raises to the caller — graph
maintenance is a background nicety layered on top of real work (reflection,
dreaming, chat tool calls); a failure here must never break any of that.
Failures are logged at debug level and swallowed.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# type ∈ person|project|fact|event|preference|place|topic|goal|device|org (spec §2.2)
VALID_NODE_TYPES = frozenset({
    "person", "project", "fact", "event", "preference", "place",
    "topic", "goal", "device", "org",
})
# relation ∈ works_on|related_to|contradicts|caused_by|prefers|part_of|
# located_in|funds|motivates|mentions|happened_at (extensible enum — spec
# §2.2 explicitly calls this out as extensible, so a relation outside this
# canonical set is still accepted, just logged at debug level).
VALID_RELATIONS = frozenset({
    "works_on", "related_to", "contradicts", "caused_by", "prefers",
    "part_of", "located_in", "funds", "motivates", "mentions", "happened_at",
})

# Relation that marks something the user might dispute (spec §2.3): written
# with a low default weight and a note, autonomously — the ask-user channel
# (Part 1, later agent) is what actually surfaces it.
CONTRADICTS_RELATION = "contradicts"
_CONTRADICTS_DEFAULT_WEIGHT = 0.2

_DB_FILENAME = "graph.db"
# Serializes connect+operate blocks within this process, mirroring
# episodic.py's per-call connection pattern rather than a long-lived handle
# (so a profile switch / HERMES_HOME override never leaves a stale handle).
_lock = threading.RLock()

_EDGE_WEIGHT_CAP = 50.0


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
        CREATE TABLE IF NOT EXISTS nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            label TEXT NOT NULL,
            label_norm TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            source_kind TEXT,
            source_ref TEXT,
            salience REAL NOT NULL DEFAULT 0.5,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_surfaced TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_type_label_norm ON nodes(type, label_norm);
        CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
            node_id UNINDEXED, label, summary,
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src_id INTEGER NOT NULL,
            dst_id INTEGER NOT NULL,
            relation TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            source_ref TEXT,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id);
        CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_dedup ON edges(src_id, dst_id, relation);

        CREATE TABLE IF NOT EXISTS graph_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orig_node_id INTEGER,
            type TEXT,
            label TEXT,
            summary TEXT,
            source_kind TEXT,
            source_ref TEXT,
            salience REAL,
            archived_at TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT ''
        );
        """
    )
    return conn


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def graph_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the ``memory.graph`` config section with defaults filled in
    (spec §2.6). Uses ``cfg_get`` with inline defaults rather than
    ``DEFAULT_CONFIG`` — mirrors ``episodic_config``'s reasoning: these keys
    are not UI-edited. Never raises.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = config if config is not None else load_config()
        return {
            "enabled": bool(cfg_get(cfg, "memory", "graph", "enabled", default=True)),
            "max_nodes": int(cfg_get(cfg, "memory", "graph", "max_nodes", default=5000) or 5000),
            "inject_neighborhood": bool(
                cfg_get(cfg, "memory", "graph", "inject_neighborhood", default=True)
            ),
            "build_in_reflection": bool(
                cfg_get(cfg, "memory", "graph", "build_in_reflection", default=True)
            ),
        }
    except Exception:
        logger.debug("graph: config read failed, using defaults", exc_info=True)
        return {
            "enabled": True,
            "max_nodes": 5000,
            "inject_neighborhood": True,
            "build_in_reflection": True,
        }


def _enabled() -> bool:
    return graph_config()["enabled"]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").strip()).lower()


def _normalize_type(type_: Optional[str]) -> str:
    value = str(type_ or "").strip().lower()
    return value if value in VALID_NODE_TYPES else "fact"


def _fts_query_string(text: str) -> str:
    terms = re.findall(r"[\w-]+", text, flags=re.UNICODE)
    if not terms:
        return ""
    return " AND ".join(f'"{t}"' for t in terms[:12])


def _node_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "type": row["type"],
        "label": row["label"],
        "summary": row["summary"] or "",
        "source_kind": row["source_kind"],
        "source_ref": row["source_ref"],
        "salience": float(row["salience"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_surfaced": row["last_surfaced"],
    }


def _edge_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "src": int(row["src_id"]),
        "dst": int(row["dst_id"]),
        "relation": row["relation"],
        "weight": float(row["weight"]),
        "source_ref": row["source_ref"],
        "note": row["note"] or "",
        "created_at": row["created_at"],
    }


def _archive_node_row(conn: sqlite3.Connection, row: sqlite3.Row, reason: str) -> None:
    """Insert ``row`` (a ``nodes`` row) into ``graph_archive``. Called under
    an open transaction, BEFORE the node itself is deleted, so the node's
    text is never lost — mirrors ``tools.memory_tool.archive_entry``'s
    never-delete guarantee for § entries."""
    conn.execute(
        "INSERT INTO graph_archive"
        "(orig_node_id, type, label, summary, source_kind, source_ref, salience, archived_at, reason) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            int(row["id"]), row["type"], row["label"], row["summary"],
            row["source_kind"], row["source_ref"], row["salience"], _now_iso(), reason,
        ),
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def upsert_node(
    type: str,
    label: str,
    summary: str = "",
    *,
    source_kind: Optional[str] = None,
    source_ref: Optional[str] = None,
    salience: float = 0.5,
) -> Optional[int]:
    """Insert or update a node. Dedup key is ``(type, normalized label)``.

    On a repeat upsert (same type + label), the summary is refreshed (kept
    if the new call passes an empty one), source_kind/source_ref are
    updated only when a new value is given, and salience nudges up slightly
    (evidence accumulates) capped at 1.0. Returns the node id, or ``None``
    on failure/disabled. Never raises.
    """
    if not _enabled():
        return None
    try:
        type_norm = _normalize_type(type)
        label = str(label or "").strip()
        if not label:
            return None
        summary = str(summary or "").strip()
        label_norm = normalize_label(label)
        salience_value = _clamp01(salience)
        now = _now_iso()
        with _lock:
            conn = _connect()
            try:
                with conn:
                    existing = conn.execute(
                        "SELECT id, summary, salience FROM nodes WHERE type = ? AND label_norm = ?",
                        (type_norm, label_norm),
                    ).fetchone()
                    if existing:
                        node_id = int(existing["id"])
                        new_summary = summary or (existing["summary"] or "")
                        new_salience = min(1.0, max(float(existing["salience"]), salience_value) + 0.02)
                        conn.execute(
                            "UPDATE nodes SET summary = ?, "
                            "source_kind = COALESCE(?, source_kind), "
                            "source_ref = COALESCE(?, source_ref), "
                            "salience = ?, updated_at = ? WHERE id = ?",
                            (new_summary, source_kind, source_ref, new_salience, now, node_id),
                        )
                        conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (node_id,))
                        conn.execute(
                            "INSERT INTO nodes_fts(node_id, label, summary) VALUES (?, ?, ?)",
                            (node_id, label, new_summary),
                        )
                    else:
                        cur = conn.execute(
                            "INSERT INTO nodes"
                            "(type, label, label_norm, summary, source_kind, source_ref, "
                            "salience, created_at, updated_at, last_surfaced) "
                            "VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                            (type_norm, label, label_norm, summary, source_kind, source_ref,
                             salience_value, now, now),
                        )
                        node_id = int(cur.lastrowid)
                        conn.execute(
                            "INSERT INTO nodes_fts(node_id, label, summary) VALUES (?, ?, ?)",
                            (node_id, label, summary),
                        )
                return node_id
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: upsert_node failed", exc_info=True)
        return None


def add_edge(
    src_id: int,
    dst_id: int,
    relation: str,
    weight: float = 1.0,
    *,
    source_ref: Optional[str] = None,
    note: str = "",
) -> Optional[int]:
    """Insert or strengthen an edge. Dedup key is ``(src_id, dst_id,
    relation)`` — a repeat call bumps the existing weight (capped) rather
    than creating a duplicate row. ``relation`` outside :data:`VALID_RELATIONS`
    is still accepted (extensible enum per spec §2.2), just logged.

    A ``contradicts`` edge with no explicit weight gets the low default
    weight (0.2) called for in spec §2.3 — "written but flagged... for now:
    just create it with a low weight and a note" (the ask-user channel that
    surfaces it is wired by a later autonomy agent). Returns the edge id, or
    ``None`` on failure/disabled/self-loop/missing endpoints. Never raises.
    """
    if not _enabled():
        return None
    try:
        src_id = int(src_id)
        dst_id = int(dst_id)
        if src_id == dst_id:
            return None
        relation = str(relation or "").strip().lower()
        if not relation:
            return None
        if relation not in VALID_RELATIONS:
            logger.debug("graph: non-canonical relation %r accepted (extensible enum)", relation)
        if relation == CONTRADICTS_RELATION and weight is None:
            weight = _CONTRADICTS_DEFAULT_WEIGHT
        weight_value = max(0.0, float(weight if weight is not None else 1.0))
        note = str(note or "").strip()
        now = _now_iso()
        with _lock:
            conn = _connect()
            try:
                endpoints = conn.execute(
                    "SELECT COUNT(*) AS c FROM nodes WHERE id IN (?, ?)", (src_id, dst_id)
                ).fetchone()
                if not endpoints or int(endpoints["c"]) < 2:
                    return None
                with conn:
                    existing = conn.execute(
                        "SELECT id, weight FROM edges WHERE src_id = ? AND dst_id = ? AND relation = ?",
                        (src_id, dst_id, relation),
                    ).fetchone()
                    if existing:
                        edge_id = int(existing["id"])
                        new_weight = min(_EDGE_WEIGHT_CAP, float(existing["weight"]) + weight_value)
                        conn.execute(
                            "UPDATE edges SET weight = ?, "
                            "source_ref = COALESCE(?, source_ref), "
                            "note = CASE WHEN ? <> '' THEN ? ELSE note END "
                            "WHERE id = ?",
                            (new_weight, source_ref, note, note, edge_id),
                        )
                    else:
                        cur = conn.execute(
                            "INSERT INTO edges(src_id, dst_id, relation, weight, source_ref, note, created_at) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (src_id, dst_id, relation, weight_value, source_ref, note, now),
                        )
                        edge_id = int(cur.lastrowid)
                return edge_id
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: add_edge failed", exc_info=True)
        return None


def edit_node(node_id: int, *, type: str, label: str, summary: str, salience: float) -> Optional[Dict[str, Any]]:
    """Update one graph node and its search index. Never raises."""
    if not _enabled():
        return None
    try:
        node_id = int(node_id)
        type_norm = _normalize_type(type)
        label = str(label or "").strip()
        summary = str(summary or "").strip()
        if not label:
            return None
        with _lock:
            conn = _connect()
            try:
                row = conn.execute("SELECT id FROM nodes WHERE id = ?", (node_id,)).fetchone()
                conflict = conn.execute(
                    "SELECT id FROM nodes WHERE type = ? AND label_norm = ? AND id <> ?",
                    (type_norm, normalize_label(label), node_id),
                ).fetchone()
                if not row or conflict:
                    return None
                with conn:
                    conn.execute(
                        "UPDATE nodes SET type = ?, label = ?, label_norm = ?, summary = ?, salience = ?, updated_at = ? WHERE id = ?",
                        (type_norm, label, normalize_label(label), summary, _clamp01(salience), _now_iso(), node_id),
                    )
                    conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (node_id,))
                    conn.execute(
                        "INSERT INTO nodes_fts(node_id, label, summary) VALUES (?, ?, ?)",
                        (node_id, label, summary),
                    )
                updated = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
                return _node_row_to_dict(updated) if updated else None
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: edit_node failed", exc_info=True)
        return None


def delete_node(node_id: int, reason: str = "deleted by user") -> bool:
    """Archive a node, then remove it and its edges from the live graph. Never raises."""
    if not _enabled():
        return False
    try:
        node_id = int(node_id)
        with _lock:
            conn = _connect()
            try:
                row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
                if not row:
                    return False
                with conn:
                    _archive_node_row(conn, row, reason=reason)
                    conn.execute("DELETE FROM edges WHERE src_id = ? OR dst_id = ?", (node_id, node_id))
                    conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (node_id,))
                    conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
                return True
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: delete_node failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_node(node_id: int) -> Optional[Dict[str, Any]]:
    """Return one node by id, or ``None`` if missing/disabled. Never raises."""
    if not _enabled():
        return None
    try:
        node_id = int(node_id)
        with _lock:
            conn = _connect()
            try:
                row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
                return _node_row_to_dict(row) if row else None
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: get_node failed", exc_info=True)
        return None


def find_node(label: str, type: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Exact-match lookup by normalized label (+ optional type). Used by the
    cheap ``record_from_episode``/``record_from_memory_entry`` helpers and
    tests. Never raises."""
    if not _enabled():
        return None
    try:
        label_norm = normalize_label(label)
        if not label_norm:
            return None
        with _lock:
            conn = _connect()
            try:
                if type:
                    row = conn.execute(
                        "SELECT * FROM nodes WHERE label_norm = ? AND type = ?",
                        (label_norm, _normalize_type(type)),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM nodes WHERE label_norm = ? ORDER BY salience DESC LIMIT 1",
                        (label_norm,),
                    ).fetchone()
                return _node_row_to_dict(row) if row else None
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: find_node failed", exc_info=True)
        return None


def query(*, text: Optional[str] = None, type: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """Search nodes: FTS over label+summary when ``text`` is given, else a
    plain type/salience-ordered listing. Never raises — ``[]`` on failure."""
    if not _enabled():
        return []
    try:
        limit = max(1, min(int(limit or 20), 200))
        type_filter = _normalize_type(type) if type else None
        with _lock:
            conn = _connect()
            try:
                if text and str(text).strip():
                    fts_q = _fts_query_string(str(text).strip())
                    if not fts_q:
                        return []
                    sql = (
                        "SELECT n.*, bm25(nodes_fts) AS score FROM nodes_fts "
                        "JOIN nodes n ON n.id = nodes_fts.node_id WHERE nodes_fts MATCH ?"
                    )
                    params: List[Any] = [fts_q]
                    if type_filter:
                        sql += " AND n.type = ?"
                        params.append(type_filter)
                    sql += " ORDER BY score, n.salience DESC LIMIT ?"
                    params.append(limit)
                else:
                    sql = "SELECT * FROM nodes WHERE 1=1"
                    params = []
                    if type_filter:
                        sql += " AND type = ?"
                        params.append(type_filter)
                    sql += " ORDER BY salience DESC, updated_at DESC LIMIT ?"
                    params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                return [_node_row_to_dict(r) for r in rows]
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: query failed", exc_info=True)
        return []


def _bfs(conn: sqlite3.Connection, node_id: int, depth: int) -> Tuple[Dict[int, Dict[str, Any]], List[Dict[str, Any]]]:
    """Breadth-first walk from ``node_id`` up to ``depth`` hops (undirected).

    Returns ``(nodes_by_id, edges)`` where ``edges`` is the INDUCED edge set
    (both endpoints within the reached node set) — the standard subgraph
    definition. ``node_id`` itself is included in the returned node map.
    """
    visited = {node_id}
    frontier = {node_id}
    for _ in range(max(0, depth)):
        if not frontier:
            break
        placeholders = ",".join("?" * len(frontier))
        params = tuple(frontier) * 2
        rows = conn.execute(
            f"SELECT src_id, dst_id FROM edges WHERE src_id IN ({placeholders}) OR dst_id IN ({placeholders})",
            params,
        ).fetchall()
        next_frontier = set()
        for r in rows:
            for nid in (int(r["src_id"]), int(r["dst_id"])):
                if nid not in visited:
                    next_frontier.add(nid)
        visited |= next_frontier
        frontier = next_frontier

    nodes: Dict[int, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    if visited:
        placeholders = ",".join("?" * len(visited))
        ids = tuple(visited)
        node_rows = conn.execute(f"SELECT * FROM nodes WHERE id IN ({placeholders})", ids).fetchall()
        nodes = {int(r["id"]): _node_row_to_dict(r) for r in node_rows}
        edge_rows = conn.execute(
            f"SELECT * FROM edges WHERE src_id IN ({placeholders}) AND dst_id IN ({placeholders})",
            ids + ids,
        ).fetchall()
        edges = [_edge_row_to_dict(r) for r in edge_rows]
    return nodes, edges


def neighbors(node_id: int, depth: int = 1) -> Dict[str, Any]:
    """Return ``{"nodes": [...], "edges": [...]}`` for the nodes/edges within
    ``depth`` hops of ``node_id`` — EXCLUDING the center node itself from
    ``nodes`` (edges still reference it by id). Depth is clamped to [1, 4].
    Never raises; empty result on failure/disabled/missing node."""
    if not _enabled():
        return {"nodes": [], "edges": []}
    try:
        node_id = int(node_id)
        depth = max(1, min(int(depth or 1), 4))
        with _lock:
            conn = _connect()
            try:
                nodes, edges = _bfs(conn, node_id, depth)
                nodes.pop(node_id, None)
                return {"nodes": list(nodes.values()), "edges": edges}
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: neighbors failed", exc_info=True)
        return {"nodes": [], "edges": []}


def subgraph(node_id: int, depth: int = 2) -> Dict[str, Any]:
    """Like :func:`neighbors` but INCLUDES the center node — the shape the
    UI's Graph tab and the ``GET /api/memory/graph`` endpoint consume."""
    if not _enabled():
        return {"nodes": [], "edges": []}
    try:
        node_id = int(node_id)
        depth = max(1, min(int(depth or 2), 4))
        with _lock:
            conn = _connect()
            try:
                nodes, edges = _bfs(conn, node_id, depth)
                return {"nodes": list(nodes.values()), "edges": edges}
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: subgraph failed", exc_info=True)
        return {"nodes": [], "edges": []}


def top_salience_subgraph(limit: int = 60) -> Dict[str, Any]:
    """The top-``limit`` highest-salience nodes plus any edges between them —
    used when the ``GET /api/memory/graph`` endpoint has no ``focus``. Never
    raises."""
    if not _enabled():
        return {"nodes": [], "edges": []}
    try:
        limit = max(1, min(int(limit or 60), 300))
        with _lock:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM nodes ORDER BY salience DESC, updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
                ids = [int(r["id"]) for r in rows]
                nodes = [_node_row_to_dict(r) for r in rows]
                edges: List[Dict[str, Any]] = []
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    id_tuple = tuple(ids)
                    edge_rows = conn.execute(
                        f"SELECT * FROM edges WHERE src_id IN ({placeholders}) AND dst_id IN ({placeholders})",
                        id_tuple + id_tuple,
                    ).fetchall()
                    edges = [_edge_row_to_dict(r) for r in edge_rows]
                return {"nodes": nodes, "edges": edges}
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: top_salience_subgraph failed", exc_info=True)
        return {"nodes": [], "edges": []}


def count() -> int:
    """Total live node count. Returns 0 on failure. Never raises."""
    try:
        with _lock:
            conn = _connect()
            try:
                row = conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()
                return int(row["c"]) if row else 0
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: count failed", exc_info=True)
        return 0


def archived(limit: int = 50) -> List[Dict[str, Any]]:
    """Recently archived nodes, newest first. Never raises."""
    try:
        limit = max(1, min(int(limit or 50), 500))
        with _lock:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM graph_archive ORDER BY archived_at DESC LIMIT ?", (limit,)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: archived failed", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Maintenance — merge + prune (dreaming consolidation, spec §2.3 last bullet)
# ---------------------------------------------------------------------------


def merge_nodes(keep_id: int, drop_id: int) -> bool:
    """Merge ``drop_id`` into ``keep_id``: edges repoint to ``keep_id``
    (de-duplicating + summing weight on collision), summaries/salience merge
    into ``keep_id``, and ``drop_id`` is archived (never hard-deleted) then
    removed. Returns True on success, False on failure/disabled/no-op
    (missing node, or keep_id == drop_id). Never raises."""
    if not _enabled():
        return False
    try:
        keep_id = int(keep_id)
        drop_id = int(drop_id)
        if keep_id == drop_id:
            return False
        with _lock:
            conn = _connect()
            try:
                keep = conn.execute("SELECT * FROM nodes WHERE id = ?", (keep_id,)).fetchone()
                drop = conn.execute("SELECT * FROM nodes WHERE id = ?", (drop_id,)).fetchone()
                if not keep or not drop:
                    return False
                with conn:
                    # Repoint drop_id's edges to keep_id one at a time (not a bulk
                    # UPDATE): the (src_id, dst_id, relation) UNIQUE index would
                    # reject a bulk UPDATE the moment a repointed edge collides
                    # with one keep_id already has, so each edge is checked for a
                    # collision first and merged (weight summed) instead of
                    # updated in that case. A repoint that would create a
                    # keep_id<->keep_id self-loop is dropped outright.
                    for side, other_side in (("src_id", "dst_id"), ("dst_id", "src_id")):
                        rows = conn.execute(f"SELECT * FROM edges WHERE {side} = ?", (drop_id,)).fetchall()
                        for row in rows:
                            other_id = int(row[other_side])
                            if other_id == keep_id:
                                conn.execute("DELETE FROM edges WHERE id = ?", (row["id"],))
                                continue
                            new_src, new_dst = (keep_id, other_id) if side == "src_id" else (other_id, keep_id)
                            existing = conn.execute(
                                "SELECT id, weight FROM edges WHERE src_id = ? AND dst_id = ? AND relation = ?",
                                (new_src, new_dst, row["relation"]),
                            ).fetchone()
                            if existing:
                                new_weight = min(_EDGE_WEIGHT_CAP, float(existing["weight"]) + float(row["weight"]))
                                conn.execute("UPDATE edges SET weight = ? WHERE id = ?", (new_weight, existing["id"]))
                                conn.execute("DELETE FROM edges WHERE id = ?", (row["id"],))
                            else:
                                conn.execute(f"UPDATE edges SET {side} = ? WHERE id = ?", (keep_id, row["id"]))
                    keep_summary = keep["summary"] or ""
                    drop_summary = drop["summary"] or ""
                    if drop_summary and drop_summary not in keep_summary:
                        merged_summary = (keep_summary + " " + drop_summary).strip()[:2000] if keep_summary else drop_summary
                    else:
                        merged_summary = keep_summary
                    merged_salience = min(1.0, max(float(keep["salience"]), float(drop["salience"])))
                    now = _now_iso()
                    conn.execute(
                        "UPDATE nodes SET summary = ?, salience = ?, updated_at = ? WHERE id = ?",
                        (merged_summary, merged_salience, now, keep_id),
                    )
                    conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (keep_id,))
                    conn.execute(
                        "INSERT INTO nodes_fts(node_id, label, summary) VALUES (?, ?, ?)",
                        (keep_id, keep["label"], merged_summary),
                    )
                    _archive_node_row(conn, drop, reason=f"merged into node {keep_id}")
                    conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (drop_id,))
                    conn.execute("DELETE FROM nodes WHERE id = ?", (drop_id,))
                return True
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: merge_nodes failed", exc_info=True)
        return False


def prune_low_salience(max_nodes: Optional[int] = None) -> int:
    """When the live node count exceeds ``max_nodes`` (default from
    ``memory.graph.max_nodes``), archive + delete the lowest-salience nodes
    beyond the cap (and their edges). Text is NEVER lost — every pruned node
    lands in ``graph_archive`` first. Returns the number of nodes pruned (0
    if under the cap, disabled, or on failure). Never raises."""
    if not _enabled():
        return 0
    try:
        cap = int(max_nodes) if max_nodes is not None else graph_config()["max_nodes"]
        cap = max(0, cap)
        with _lock:
            conn = _connect()
            try:
                total_row = conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()
                total = int(total_row["c"]) if total_row else 0
                overflow = total - cap
                if overflow <= 0:
                    return 0
                rows = conn.execute(
                    "SELECT * FROM nodes ORDER BY salience ASC, updated_at ASC LIMIT ?", (overflow,)
                ).fetchall()
                with conn:
                    for row in rows:
                        _archive_node_row(conn, row, reason="pruned: low salience beyond max_nodes cap")
                        node_id = int(row["id"])
                        conn.execute("DELETE FROM edges WHERE src_id = ? OR dst_id = ?", (node_id, node_id))
                        conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (node_id,))
                        conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
                return len(rows)
            finally:
                conn.close()
    except Exception:
        logger.debug("graph: prune_low_salience failed", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Formatting — shared by recall_graph (tools/graph_tool.py) and the prompt
# neighborhood-injection helper (agent/memory/graph_builder.py).
# ---------------------------------------------------------------------------


def format_neighborhood(center: Dict[str, Any], edges: List[Dict[str, Any]], nodes_by_id: Dict[int, Dict[str, Any]]) -> str:
    """Render a node's edges as readable relation lines, e.g. ``NeuDocs
    —built_with→ Marvi; bakery-job —funds→ NeuDocs``. ``nodes_by_id`` should
    include ``center`` itself (keyed by its id) alongside its neighbors.
    Never raises — degrades to an empty-connections line."""
    try:
        if not edges:
            return f"{center.get('label', '?')} has no recorded connections yet."
        center_id = center.get("id")
        by_id = dict(nodes_by_id)
        if center_id is not None:
            by_id.setdefault(center_id, center)
        parts: List[str] = []
        for edge in edges:
            src = by_id.get(edge.get("src"), {})
            dst = by_id.get(edge.get("dst"), {})
            src_label = src.get("label", "?")
            dst_label = dst.get("label", "?")
            parts.append(f"{src_label} —{edge.get('relation', 'related_to')}→ {dst_label}")
        return "; ".join(parts)
    except Exception:
        logger.debug("graph: format_neighborhood failed", exc_info=True)
        return f"{center.get('label', '?')} — connections unavailable."
