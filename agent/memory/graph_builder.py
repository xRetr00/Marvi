"""Graph population — builds Marvi's knowledge graph from existing memory
(spec §2.3, ``docs/superpowers/specs/2026-07-20-marvi-freedom-and-graph-mind-spec.md``).

Two paths, by design:

  1. CHEAP, always-on: :func:`record_from_episode` / :func:`record_from_memory_entry`
     — no LLM call. An episode or a § memory entry upserts its own node
     (+ a lightweight ``mentions``/``part_of`` edge to its entities/topic)
     directly. World/episodic writers can call these on every write without
     worrying about cost or latency.
  2. DEEPER, LLM-assisted, bounded batch: :func:`build_graph_from_memory` —
     reads recent semantic (``tools.memory_tool.MemoryStore``) + episodic
     entries not yet ingested (idempotent by source_ref via a cursor at
     ``HERMES_HOME/memory/graph_builder_state.json``) and extracts
     entities + typed relations via the auxiliary model
     (``auxiliary.graph_builder.{provider,model}``, falling back to the
     same task-routed selector ``agent/background_review.py`` and
     ``agent/title_generator.py`` use — ``agent.auxiliary_client.call_llm``
     with ``task=...`` reads ``auxiliary.<task>.*`` from config and falls
     back to the main runtime / auto-detection chain when unset). Only
     high-confidence relations are written; a ``contradicts`` relation is
     still written (autonomous — it references sources and decay/merge can
     clean it up) but with a low weight and a note, per spec §2.3 — the
     ask-user channel that actually surfaces it for the user is wired by a
     later (Part 1) autonomy agent.

Wired into the nightly reflection job (``cron/subconscious.py``) behind
``memory.graph.build_in_reflection`` (default true).

Every public function is guarded: never raises, degrades to a no-op/empty
result on any failure (disabled config, missing aux provider, malformed
model output, ...).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from utils import atomic_replace

from agent.memory import graph

logger = logging.getLogger(__name__)

# Edges below this confidence are dropped (spec §2.3: "only high-confidence
# relations are written"). contradicts edges are exempt — they're written
# regardless of confidence, at a low weight, precisely because they're the
# case worth flagging rather than silently dropping.
_MIN_EDGE_CONFIDENCE = 0.6

# Bounded batch size per build_graph_from_memory() call.
_DEFAULT_BATCH_LIMIT = 40
_MAX_PROMPT_CHARS = 12_000

# Cheap text-similarity threshold for the dreaming duplicate-merge pass
# (difflib, no embeddings -- mirrors agent.memory.decay.text_similarity's
# approach for the flat memory store).
_DUPLICATE_LABEL_SIMILARITY = 0.88
_DUPLICATE_SCAN_LIMIT_PER_TYPE = 500

_GRAPH_BUILDER_SYSTEM = (
    "You extract a compact personal knowledge graph from short memory/episode "
    "snippets about one user. Each input line is prefixed with a [REF:...] tag "
    "identifying its source.\n\n"
    "Return ONLY a JSON object (no prose, no markdown fences) shaped exactly like:\n"
    '{"nodes": [{"type": "person|project|fact|event|preference|place|topic|goal|'
    'device|org", "label": "short name", "summary": "one sentence", '
    '"source_ref": "the REF tag this came from, if clear"}], '
    '"edges": [{"src": "label of source node", "dst": "label of target node", '
    '"relation": "works_on|related_to|contradicts|caused_by|prefers|part_of|'
    'located_in|funds|motivates|mentions|happened_at", '
    '"confidence": 0.0-1.0, "note": "short reason, required for contradicts", '
    '"source_ref": "the REF tag this came from, if clear"}]}\n\n'
    "Rules: only extract relations clearly stated or strongly implied by the "
    "text — never invent. Reuse the SAME label string for the same entity "
    "across nodes/edges so they link up. A 'contradicts' edge marks something "
    "the user would dispute if told (e.g. a newer fact conflicting with an "
    "older one) — still include it, with confidence reflecting your certainty "
    "and a short 'note' explaining the conflict; it will be written at a low "
    "weight for later review rather than acted on automatically. Keep the "
    "graph small and precise, not exhaustive."
)


# ---------------------------------------------------------------------------
# Cheap, always-on path (no LLM)
# ---------------------------------------------------------------------------


def record_from_episode(episode: Dict[str, Any]) -> Optional[int]:
    """Cheaply upsert an episode as an 'event' node plus 'mentions' edges to
    its entities — no LLM call. Safe to call on every episode write. Returns
    the event node id, or ``None`` on failure/disabled/empty title."""
    if not graph.graph_config()["enabled"]:
        return None
    try:
        episode = episode or {}
        title = str(episode.get("title") or "").strip()
        if not title:
            return None
        episode_id = episode.get("id")
        source_ref = f"episode:{episode_id}" if episode_id is not None else None
        event_id = graph.upsert_node(
            type="event",
            label=title,
            summary=str(episode.get("summary") or "")[:500],
            source_kind="episode",
            source_ref=source_ref,
            salience=0.4,
        )
        if event_id is None:
            return None
        for entity in episode.get("entities") or []:
            entity = str(entity).strip()
            if not entity:
                continue
            entity_id = graph.upsert_node(
                type="topic",
                label=entity,
                source_kind="episode",
                source_ref=source_ref,
                salience=0.3,
            )
            if entity_id is not None:
                graph.add_edge(event_id, entity_id, "mentions", weight=1.0, source_ref=source_ref)
        return event_id
    except Exception:
        logger.debug("graph_builder: record_from_episode failed", exc_info=True)
        return None


def record_from_memory_entry(text: str, topic: Optional[str] = None) -> Optional[int]:
    """Cheaply upsert a § memory entry as a 'fact' node plus a 'part_of' edge
    to its topic — no LLM call. Returns the fact node id, or ``None`` on
    failure/disabled/empty text."""
    if not graph.graph_config()["enabled"]:
        return None
    try:
        from tools.memory_tool import entry_hash, split_topic

        text = str(text or "").strip()
        if not text:
            return None
        parsed_topic, body = split_topic(text)
        topic_value = (topic or parsed_topic or "Uncategorized").strip() or "Uncategorized"
        label = (body or text).strip()[:120]
        source_ref = f"memory:{entry_hash(text)}"
        fact_id = graph.upsert_node(
            type="fact",
            label=label,
            summary=text[:500],
            source_kind="memory",
            source_ref=source_ref,
            salience=0.4,
        )
        if fact_id is None:
            return None
        topic_id = graph.upsert_node(
            type="topic",
            label=topic_value,
            source_kind="memory",
            source_ref=source_ref,
            salience=0.3,
        )
        if topic_id is not None:
            graph.add_edge(fact_id, topic_id, "part_of", weight=1.0, source_ref=source_ref)
        return fact_id
    except Exception:
        logger.debug("graph_builder: record_from_memory_entry failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Neighborhood injection helper — standalone; MemoryStore's prompt injection
# MAY call this (spec §2.4). Never mutates MemoryStore's own ranking/state.
# ---------------------------------------------------------------------------


def graph_neighborhood_for_context(entities: List[str], budget: int = 600) -> str:
    """Bounded "relevant connections" block for entities present in the
    current context. Standalone: MemoryStore's system-prompt rendering MAY
    call this (behind ``memory.graph.inject_neighborhood``, default true)
    and append the result after its own block. Returns "" when disabled, no
    entity has a graph node, or on any failure. Never raises."""
    try:
        cfg = graph.graph_config()
        if not cfg["enabled"] or not cfg["inject_neighborhood"]:
            return ""
        entities = [str(e).strip() for e in (entities or []) if str(e).strip()]
        if not entities:
            return ""
        lines: List[str] = []
        seen_nodes: set = set()
        total = 0
        for entity in entities[:8]:
            node = graph.find_node(entity)
            if not node or node["id"] in seen_nodes:
                continue
            seen_nodes.add(node["id"])
            neigh = graph.neighbors(node["id"], depth=1)
            if not neigh["edges"]:
                continue
            nodes_by_id = {n["id"]: n for n in neigh["nodes"]}
            nodes_by_id[node["id"]] = node
            line = f"- {graph.format_neighborhood(node, neigh['edges'][:6], nodes_by_id)}"
            if total + len(line) > budget:
                break
            lines.append(line)
            total += len(line)
        if not lines:
            return ""
        return "Relevant connections (graph memory):\n" + "\n".join(lines)
    except Exception:
        logger.debug("graph_builder: graph_neighborhood_for_context failed", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Cursor state (idempotency by source_ref)
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    return get_hermes_home() / "memory" / "graph_builder_state.json"


def _load_state() -> Dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".graphstate_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            atomic_replace(tmp_path, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception:
        logger.debug("graph_builder: failed to persist cursor state", exc_info=True)


def _collect_unprocessed_items(state: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """Gather up to ``limit`` not-yet-ingested items: episodes newer than the
    cursor's ``last_episode_id`` (ascending), then § memory entries whose
    hash isn't in ``processed_memory_hashes``. Best-effort per source."""
    items: List[Dict[str, Any]] = []
    last_episode_id = int(state.get("last_episode_id") or 0)
    try:
        from agent.memory.episodic import query as query_episodes

        episodes = query_episodes(limit=200)  # newest first
        pending = sorted(
            (e for e in episodes if int(e.get("id") or 0) > last_episode_id),
            key=lambda e: int(e["id"]),
        )
        for ep in pending:
            if len(items) >= limit:
                break
            text = f"{ep.get('title', '')} — {ep.get('summary', '')}".strip(" —")
            if not text:
                continue
            items.append({"ref": f"episode:{ep['id']}", "text": text, "kind": "episode", "episode_id": int(ep["id"])})
    except Exception:
        logger.debug("graph_builder: episodic collection failed", exc_info=True)

    if len(items) < limit:
        try:
            from tools.memory_tool import entry_hash, load_on_disk_store

            processed_hashes = set(state.get("processed_memory_hashes") or [])
            store = load_on_disk_store()
            for target in ("user", "memory"):
                for text in store._entries_for(target):
                    if len(items) >= limit:
                        break
                    h = entry_hash(text)
                    if h in processed_hashes:
                        continue
                    items.append({"ref": f"memory:{h}", "text": text, "kind": "memory", "hash": h})
        except Exception:
            logger.debug("graph_builder: memory collection failed", exc_info=True)

    return items


def _advance_state(state: Dict[str, Any], items: List[Dict[str, Any]]) -> None:
    max_episode_id = int(state.get("last_episode_id") or 0)
    hashes = set(state.get("processed_memory_hashes") or [])
    for item in items:
        if item["kind"] == "episode":
            max_episode_id = max(max_episode_id, int(item["episode_id"]))
        elif item["kind"] == "memory":
            hashes.add(item["hash"])
    state["last_episode_id"] = max_episode_id
    trimmed = list(hashes)
    if len(trimmed) > 5000:
        trimmed = trimmed[-5000:]
    state["processed_memory_hashes"] = trimmed


def _parse_json_object(content: str) -> Optional[Dict[str, Any]]:
    content = (content or "").strip()
    if not content:
        return None
    try:
        data = json.loads(content)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        pass
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(content[start:end + 1])
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def _process_extraction(data: Dict[str, Any], default_source_ref: str) -> Dict[str, int]:
    """Write extracted nodes/edges to the graph store. Only high-confidence
    edges are written, except ``contradicts`` (always written, low weight —
    spec §2.3). Returns counters for the caller's summary."""
    stats = {"nodes": 0, "edges": 0, "flagged": 0}
    label_to_id: Dict[str, int] = {}

    for node in data.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        label = str(node.get("label") or "").strip()
        if not label:
            continue
        node_type = str(node.get("type") or "fact").strip().lower()
        summary = str(node.get("summary") or "").strip()
        source_ref = str(node.get("source_ref") or "").strip() or default_source_ref
        node_id = graph.upsert_node(
            type=node_type, label=label, summary=summary,
            source_kind="graph_builder", source_ref=source_ref, salience=0.45,
        )
        if node_id is not None:
            label_to_id[graph.normalize_label(label)] = node_id
            stats["nodes"] += 1

    for edge in data.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        src_label_raw = str(edge.get("src") or "").strip()
        dst_label_raw = str(edge.get("dst") or "").strip()
        if not src_label_raw or not dst_label_raw:
            continue
        relation = str(edge.get("relation") or "related_to").strip().lower()
        try:
            confidence = float(edge.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.5
        is_contradiction = relation == graph.CONTRADICTS_RELATION
        if not is_contradiction and confidence < _MIN_EDGE_CONFIDENCE:
            continue  # spec §2.3: only high-confidence relations are written

        src_id = label_to_id.get(graph.normalize_label(src_label_raw))
        if src_id is None:
            found = graph.find_node(src_label_raw)
            src_id = found["id"] if found else None
        dst_id = label_to_id.get(graph.normalize_label(dst_label_raw))
        if dst_id is None:
            found = graph.find_node(dst_label_raw)
            dst_id = found["id"] if found else None
        if not src_id or not dst_id:
            continue

        source_ref = str(edge.get("source_ref") or "").strip() or default_source_ref
        note = str(edge.get("note") or "").strip()
        weight = 0.2 if is_contradiction else min(1.0, max(0.1, confidence))
        edge_id = graph.add_edge(src_id, dst_id, relation, weight=weight, source_ref=source_ref, note=note)
        if edge_id is not None:
            stats["edges"] += 1
            if is_contradiction:
                stats["flagged"] += 1

    return stats


def build_graph_from_memory(limit: int = _DEFAULT_BATCH_LIMIT) -> Dict[str, Any]:
    """Bounded batch pass: read not-yet-ingested semantic + episodic entries,
    extract entities/relations via the auxiliary model, and write
    high-confidence results to the graph store. Idempotent — a cursor at
    ``HERMES_HOME/memory/graph_builder_state.json`` skips already-ingested
    entries on the next call, so this is safe to call from the nightly
    reflection job AND on demand.

    Never raises; returns a summary dict for logging/tests (``enabled``,
    ``items_processed``, ``nodes``, ``edges``, ``flagged_contradictions``,
    ``errors``).
    """
    result: Dict[str, Any] = {
        "enabled": False,
        "items_processed": 0,
        "nodes": 0,
        "edges": 0,
        "flagged_contradictions": 0,
        "errors": 0,
    }
    try:
        cfg = graph.graph_config()
        result["enabled"] = cfg["enabled"]
        if not cfg["enabled"]:
            return result

        limit = max(1, min(int(limit or _DEFAULT_BATCH_LIMIT), 200))
        state = _load_state()
        items = _collect_unprocessed_items(state, limit)
        if not items:
            return result

        prompt_text = "\n\n".join(f"[REF:{item['ref']}]\n{item['text']}" for item in items)
        messages = [
            {"role": "system", "content": _GRAPH_BUILDER_SYSTEM},
            {"role": "user", "content": prompt_text[:_MAX_PROMPT_CHARS]},
        ]
        try:
            from agent.auxiliary_client import call_llm

            response = call_llm(task="graph_builder", messages=messages, max_tokens=2000, temperature=0.2)
            content = response.choices[0].message.content or ""
        except Exception:
            logger.debug("graph_builder: auxiliary LLM call failed", exc_info=True)
            result["errors"] += 1
            # Still advance the cursor? No -- leave items unconsumed so a
            # transient aux-provider failure doesn't silently skip them.
            return result

        data = _parse_json_object(content)
        if data:
            default_ref = f"graph_builder:batch:{datetime.now(timezone.utc).isoformat()}"
            stats = _process_extraction(data, default_ref)
            result["nodes"] = stats["nodes"]
            result["edges"] = stats["edges"]
            result["flagged_contradictions"] = stats["flagged"]
        else:
            logger.debug("graph_builder: model output was not parseable JSON")

        result["items_processed"] = len(items)
        _advance_state(state, items)
        _save_state(state)
    except Exception:
        logger.debug("graph_builder: build_graph_from_memory failed", exc_info=True)
        result["errors"] += 1
    return result


# ---------------------------------------------------------------------------
# Dreaming consolidation — duplicate-node merge (spec §2.3 last bullet).
# Called from cron/subconscious.py's dreaming graph-maintenance hook, after
# prune_low_salience(). No LLM call: cheap difflib similarity, same style as
# agent.memory.decay's dedup pass for the flat memory store.
# ---------------------------------------------------------------------------


def merge_duplicate_graph_nodes(limit_per_type: int = _DUPLICATE_SCAN_LIMIT_PER_TYPE) -> int:
    """Merge near-duplicate nodes within the same type (cheap difflib label
    similarity, no embeddings). The higher-salience node of each pair
    survives; the other is merged into it via ``agent.memory.graph.merge_nodes``
    (archived, never hard-deleted). Returns the number of merges performed
    (0 if disabled or on failure). Never raises."""
    if not graph.graph_config()["enabled"]:
        return 0
    try:
        import difflib

        merges = 0
        for node_type in sorted(graph.VALID_NODE_TYPES):
            nodes = graph.query(type=node_type, limit=limit_per_type)
            merged_ids: set = set()
            for i in range(len(nodes)):
                node_a = nodes[i]
                if node_a["id"] in merged_ids:
                    continue
                for j in range(i + 1, len(nodes)):
                    node_b = nodes[j]
                    if node_b["id"] in merged_ids:
                        continue
                    ratio = difflib.SequenceMatcher(
                        None,
                        graph.normalize_label(node_a["label"]),
                        graph.normalize_label(node_b["label"]),
                    ).ratio()
                    if ratio < _DUPLICATE_LABEL_SIMILARITY:
                        continue
                    if node_a["salience"] >= node_b["salience"]:
                        keep, drop = node_a, node_b
                    else:
                        keep, drop = node_b, node_a
                    if graph.merge_nodes(keep["id"], drop["id"]):
                        merged_ids.add(drop["id"])
                        merges += 1
        return merges
    except Exception:
        logger.debug("graph_builder: merge_duplicate_graph_nodes failed", exc_info=True)
        return 0
