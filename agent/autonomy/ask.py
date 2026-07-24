"""The ask-user channel — spec §1.4.

Marvi asking the user something, unprompted, when it's genuinely useful —
delivered through the SAME proactive delivery path as any other cron/tick
message (a one-shot cron job with ``deliver=<platform>``, which already
flows through ``gateway/flow_gate.py``'s quiet-during-focus-app hold — see
``_is_deep_work_now`` below for how the pre-check reuses that module
read-only). Budgeted via ``agent.autonomy.budget`` (category ``ask_user``),
separately hard-rate-limited (``autonomy.ask.max_per_day``), deduped against
open questions, and persisted so a later reply can be correlated back.

Correlation design note (spec §1.4 asks for "the less invasive" of two
options): rather than adding a hook into the live inbound-message path
(gateway/run.py), :func:`reconcile_pending_questions` runs at reflection
time and matches any user-authored episodic activity recorded since a
question was asked. This is a heuristic ("the user said *something* since
being asked" — not semantic verification that they answered *this*
question), documented here and at the call site
(``cron/scheduler.py``'s autonomy hook). Picked over a message-path hook
because it touches zero files on the hot inbound-message path and reuses
machinery that already exists (episodic memory).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from utils import atomic_replace

from agent.autonomy import budget

logger = logging.getLogger(__name__)

_PENDING_FILENAME = "pending_questions.json"
_MAX_STORED_QUESTIONS = 200
_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def pending_questions_path() -> Path:
    return get_hermes_home() / "autonomy" / _PENDING_FILENAME


def _empty_pending() -> Dict[str, Any]:
    return {"questions": []}


def _load_pending() -> Dict[str, Any]:
    path = pending_questions_path()
    if not path.exists():
        return _empty_pending()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_pending()
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        return _empty_pending()
    return data


def _save_pending(state: Dict[str, Any]) -> None:
    questions = state.get("questions") or []
    if len(questions) > _MAX_STORED_QUESTIONS:
        # Keep every still-pending question plus the most recent resolved
        # ones, so an old open question never silently falls off the edge.
        pending_rows = [q for q in questions if q.get("status") == "pending"]
        resolved_rows = [q for q in questions if q.get("status") != "pending"]
        keep_resolved = max(0, _MAX_STORED_QUESTIONS - len(pending_rows))
        state["questions"] = pending_rows + resolved_rows[-keep_resolved:]

    path = pending_questions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pending_q_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_pending_questions(*, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return stored questions (newest first), optionally filtered by
    status. Never raises."""
    try:
        with _lock:
            rows = list(_load_pending()["questions"])
        rows.reverse()
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        return rows
    except Exception:
        logger.debug("autonomy ask: list_pending_questions failed", exc_info=True)
        return []


def _dedup_key(question: str) -> str:
    return " ".join(question.strip().casefold().split())


def _count_asked_today(pending: Dict[str, Any]) -> int:
    today = date.today().isoformat()
    count = 0
    for row in pending.get("questions") or []:
        asked_at = str(row.get("asked_at") or "")
        if asked_at.startswith(today):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Deep-work quiet window — read-only reuse of gateway/flow_gate.py's public
# should_gate() as the "is now a bad time" signal (see module docstring).
# flow_gate.py itself is owned by another workstream and not edited here.
# ---------------------------------------------------------------------------


def _is_deep_work_now() -> bool:
    try:
        from gateway.flow_gate import should_gate

        # should_gate() only ever gates when metadata carries a job_id (its
        # "is this cron-originated" signal) — a synthetic probe id is enough
        # to ask "would a cron delivery be held right now?" without actually
        # creating one.
        return bool(should_gate({"job_id": "autonomy-ask-probe"}))
    except Exception:
        logger.debug("autonomy ask: deep-work probe failed; assuming not deep work", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Delivery target — mirrors tools/presence/goblin.py's _pick_delivery_target
# (best-effort: first connected platform with a configured home channel).
# Small enough, and specific enough to this call site, to keep as its own
# copy rather than importing a private helper from another module.
# ---------------------------------------------------------------------------


def pick_delivery_target() -> Optional[str]:
    """Best-effort: the first connected platform with a configured home
    channel. Public (not module-private) because other autonomy-adjacent
    callers with the same "deliver a proactive message" need — e.g.
    ``plugins/uni_portal/check.py``'s daily-check notification — reuse this
    rather than each keeping their own copy."""
    try:
        from gateway.config import load_gateway_config

        cfg = load_gateway_config()
        for platform in cfg.get_connected_platforms():
            if cfg.get_home_channel(platform):
                return platform.value
    except Exception:
        logger.debug("autonomy ask: could not resolve a delivery target", exc_info=True)
    return None


def _build_ask_prompt(question: str, context: str) -> str:
    parts = [
        "You are Marvi. You decided on your own that this question is worth "
        "proactively asking the user right now, rather than waiting for them "
        "to bring it up. Send it as ONE short, warm message in your own "
        "voice — keep the meaning exactly, but don't paste it robotically.",
        "",
        f"QUESTION TO ASK: {question}",
    ]
    if context:
        parts.append(f"WHY YOU'RE ASKING: {context}")
    parts.append(
        "\nSend exactly one message containing this question. Do not pad it "
        "with unrelated commentary and do not ask more than this one thing."
    )
    return "\n".join(parts)


def _create_ask_job(question: str, context: str, target: str) -> Optional[Dict[str, Any]]:
    try:
        from cron.jobs import create_job

        prompt = _build_ask_prompt(question, context)
        return create_job(
            prompt=prompt,
            schedule="1m",
            name=f"autonomy-ask-{uuid.uuid4().hex[:8]}",
            repeat=1,
            deliver=target,
        )
    except Exception:
        logger.debug("autonomy ask: failed to create delivery job", exc_info=True)
        return None


def _log_ask_activity(question: str, category: str, *, delivered: bool) -> None:
    try:
        from cron.scheduler import record_subconscious_activity

        record_subconscious_activity(
            source="autonomy",
            outcome="message" if delivered else "no_change",
            summary=f"autonomy ask ({category}): {question[:160]}",
        )
    except Exception:
        logger.debug("autonomy ask: activity log failed", exc_info=True)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def ask_user(
    question: str,
    context: str = "",
    category: str = "general",
    *,
    urgent: bool = False,
) -> Optional[Dict[str, Any]]:
    """Ask the user ``question`` through the normal proactive delivery path.

    Returns the created pending-question record on success, or ``None`` if
    the ask was skipped for any reason (autonomy disabled, budget exhausted,
    ``autonomy.ask.max_per_day`` hit, a duplicate of an already-open
    question, no configured delivery target, or a hard failure). Never
    raises — every autonomous ask attempt is best-effort.
    """
    question = str(question or "").strip()
    context = str(context or "").strip()
    category = str(category or "general").strip() or "general"
    if not question:
        return None
    try:
        cfg = budget.autonomy_config()
        if not cfg["enabled"]:
            return None
        ask_cfg = cfg["ask"]
        if not urgent and ask_cfg["quiet_in_deep_work"] and _is_deep_work_now():
            logger.debug("autonomy ask: deferred — user appears to be in deep work")
            return None

        dedup_key = _dedup_key(question)
        with _lock:
            pending = _load_pending()
            for row in pending["questions"]:
                if row.get("status") == "pending" and row.get("dedup_key") == dedup_key:
                    return None
            if _count_asked_today(pending) >= ask_cfg["max_per_day"]:
                return None

            if not budget.try_spend("ask_user", log_activity=False):
                return None

            target = pick_delivery_target()
            if not target:
                return None
            job = _create_ask_job(question, context, target)
            if job is None:
                return None

            record = {
                "id": uuid.uuid4().hex[:12],
                "question": question,
                "context": context,
                "category": category,
                "dedup_key": dedup_key,
                "status": "pending",
                "asked_at": _now_iso(),
                "target": target,
                "job_id": job.get("id"),
                "answered_at": None,
                "answer_text": None,
            }
            pending["questions"].append(record)
            _save_pending(pending)

        _log_ask_activity(question, category, delivered=True)
        return record
    except Exception:
        logger.debug("autonomy ask: ask_user failed", exc_info=True)
        return None


def reconcile_pending_questions(*, lookback_limit: int = 5) -> int:
    """Best-effort correlation pass: for each still-open pending question,
    check for user-authored episodic activity recorded since it was asked
    and, if any exists, mark the question answered (capturing the nearest
    matching episode's text as a heuristic "answer").

    Intended to be called periodically from reflection/dreaming (see
    ``cron/scheduler.py``'s autonomy hook) — see the module docstring for
    why this reflection-time pass was chosen over a live message-path hook.
    Returns the number of questions marked answered. Never raises.
    """
    try:
        with _lock:
            pending = _load_pending()
            changed = 0
            for row in pending["questions"]:
                if row.get("status") != "pending":
                    continue
                asked_at = row.get("asked_at")
                if not asked_at:
                    continue
                try:
                    from agent.memory.episodic import query as query_episodes

                    matches = query_episodes(kind="conversation", since=str(asked_at), limit=lookback_limit)
                except Exception:
                    matches = []
                user_matches = [m for m in matches if m.get("actor") == "user"]
                if user_matches:
                    row["status"] = "answered"
                    row["answered_at"] = _now_iso()
                    text = str(user_matches[0].get("summary") or user_matches[0].get("title") or "").strip()
                    row["answer_text"] = text[:1000]
                    changed += 1
            if changed:
                _save_pending(pending)
            return changed
    except Exception:
        logger.debug("autonomy ask: reconcile_pending_questions failed", exc_info=True)
        return 0


def expire_stale_questions(*, max_age_days: int = 14) -> int:
    """Mark pending questions older than ``max_age_days`` as expired (never
    deleted) so a stale open question doesn't block dedup forever. Never
    raises."""
    try:
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
        with _lock:
            pending = _load_pending()
            changed = 0
            for row in pending["questions"]:
                if row.get("status") != "pending":
                    continue
                try:
                    asked_ts = datetime.fromisoformat(str(row.get("asked_at"))).timestamp()
                except (TypeError, ValueError):
                    continue
                if asked_ts < cutoff:
                    row["status"] = "expired"
                    changed += 1
            if changed:
                _save_pending(pending)
            return changed
    except Exception:
        logger.debug("autonomy ask: expire_stale_questions failed", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Graph contradicts-edge surfacing (spec §1.4: "graph contradicts-edges
# through this channel"). Uses ONLY agent.memory.graph's public read API
# (top_salience_subgraph) — that module is owned by the graph-mind
# workstream and is read-only here. Limitation, documented: this only finds
# contradicts edges whose both endpoint nodes are currently in the
# top-salience subgraph, not an exhaustive scan of every contradicts edge —
# an exhaustive scan would need a new public "edges by relation" query on
# agent/memory/graph.py, which is out of scope for this change.
# ---------------------------------------------------------------------------


def surface_graph_contradictions(*, limit: int = 1, scan_limit: int = 200) -> int:
    """Ask the user about at most ``limit`` not-yet-asked-about
    ``contradicts`` graph edges. Returns the number of asks actually sent.
    Never raises."""
    try:
        from agent.memory import graph as graph_store

        sub = graph_store.top_salience_subgraph(limit=scan_limit)
        nodes_by_id = {n["id"]: n for n in sub.get("nodes") or []}
        contradicts = [e for e in sub.get("edges") or [] if e.get("relation") == "contradicts"]
        asked = 0
        for edge in contradicts:
            if asked >= limit:
                break
            src = nodes_by_id.get(edge.get("src"), {})
            dst = nodes_by_id.get(edge.get("dst"), {})
            src_label = src.get("label") or "?"
            dst_label = dst.get("label") or "?"
            note = edge.get("note") or ""
            question = (
                f"I noticed something that might conflict in what I know: "
                f"\"{src_label}\" vs \"{dst_label}\"" + (f" ({note})" if note else "") +
                ". Which one still holds — or are they not actually in conflict?"
            )
            record = ask_user(question, context="graph contradiction", category="contradiction")
            if record is not None:
                asked += 1
        return asked
    except Exception:
        logger.debug("autonomy ask: surface_graph_contradictions failed", exc_info=True)
        return 0
