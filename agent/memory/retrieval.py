"""Adaptive retrieval / retrospective reflection -- Loop 4 of the "memory
maturity" round (see
``docs/superpowers/specs/2026-07-17-marvi-memory-maturity-spec.md``).

Today ``tools.memory_tool.MemoryStore`` injects its ENTIRE frozen snapshot
into every prompt in fixed (insertion) order -- it never learns that some
entries are more useful than others. This module adds a soft, bounded
usefulness signal per § entry and uses it to bias *ordering* (and, only when
the char budget genuinely forces a choice, *selection*) of entries at render
time. It NEVER deletes or archives an entry -- that remains decay's job
(``agent/memory/decay.py``, Loop 3); a downweighted entry can always recover
because weights are clamped to a floor, never driven to zero.

Module boundary: this is a standalone module (not a section bolted onto
``tools/memory_tool.py``) so the store class stays lean and the usefulness
math/storage is independently unit-testable. ``tools/memory_tool.py`` only
gets two small, additive hooks: ``MemoryStore._rank_for_render()`` (calls
``rank_entries`` here) and one call to ``capture_previous_batch_outcome()``
from ``format_for_system_prompt()``. All imports are lazy/deferred on both
sides specifically to avoid a module-load-time circular import between the
two files (mirrors the existing ``agent/memory/decay.py`` <->
``tools/memory_tool.py`` pattern).

Injection -> outcome linkage (read this before touching the capture logic)
---------------------------------------------------------------------------
The spec's ideal is per-TURN linkage: know exactly which entries were
injected into the turn that just got a correction. That isn't cleanly
available here without invasive changes to the live turn path: MemoryStore's
system-prompt snapshot is frozen once per SESSION (the whole point of the
frozen-snapshot design is prefix-cache stability -- see
``tools/memory_tool.py``'s module docstring), and
``format_for_system_prompt()`` only stamps the decay sidecar
(``memory/surfaced.json``) the FIRST time it's called per target per store
instance. So the finest granularity actually available without touching the
turn/voice path is per-SESSION, not per-turn.

This module embraces that and documents it as the approximation:

  * ``capture_previous_batch_outcome()`` runs once per store instance, at
    the START of a session (from inside ``format_for_system_prompt()``,
    before it stamps THIS session's fresh batch). It looks at the *previous*
    session's surfaced batch -- the entries sharing the newest
    ``last_surfaced`` timestamp in ``memory/surfaced.json`` -- and checks
    whether a correction was recorded (``agent.learning.outcomes``,
    ``loop="escalation", event="corrected"``) any time since that
    timestamp. If yes, those entries get nudged DOWN by ``learning_rate``;
    if the session closed clean, they get nudged UP.

This reuses the EXISTING correction signal (the same ``is_correction()``
detection the escalation loop already uses in the voice duplex handler,
``hermes_cli/web_server.py``) without adding any new call into that -- or
any other -- turn-processing / voice code path. It is intentionally
coarser than per-turn (an entire session's surfaced set moves together),
and intentionally best-effort: any failure here is logged and swallowed,
never surfaced to the caller, and never affects the live turn.
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

logger = logging.getLogger(__name__)

# Mirrors tools.memory_tool.ENTRY_DELIMITER -- duplicated as a literal
# (rather than imported at module load) to keep this module importable
# standalone with zero load-time coupling to tools.memory_tool.
_ENTRY_DELIMITER = "\n§\n"

# Weights are soft nudges, never allowed to reach either extreme: MIN_WEIGHT
# is the "floor" the spec calls for -- a heavily downweighted entry can
# never be driven to zero usefulness, so a single future positive nudge
# always starts moving it back up, and it never becomes literally invisible
# to the (weight-descending, stable) sort in rank_entries().
MIN_WEIGHT = 0.2
MAX_WEIGHT = 2.0
DEFAULT_WEIGHT = 1.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def retrieval_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the ``memory.retrieval`` config section with defaults filled
    in. Uses ``cfg_get`` with inline defaults (mirrors ``decay_config`` in
    ``agent/memory/decay.py``) rather than ``DEFAULT_CONFIG`` -- these keys
    are not UI-edited. Never raises; falls back to defaults on any
    config-read failure.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = config if config is not None else load_config()
        return {
            "adaptive": bool(cfg_get(cfg, "memory", "retrieval", "adaptive", default=True)),
            "learning_rate": float(
                cfg_get(cfg, "memory", "retrieval", "learning_rate", default=0.1) or 0.1
            ),
        }
    except Exception:
        logger.debug("retrieval: config read failed, using defaults", exc_info=True)
        return {"adaptive": True, "learning_rate": 0.1}


# ---------------------------------------------------------------------------
# Usefulness store -- HERMES_HOME/memory/usefulness.json, entry-hash keyed
# (reuses tools.memory_tool.entry_hash so keys line up 1:1 with the surfaced
# sidecar and the archive).
# ---------------------------------------------------------------------------


def _usefulness_path() -> Path:
    return get_hermes_home() / "memory" / "usefulness.json"


def _clamp(weight: float) -> float:
    return max(MIN_WEIGHT, min(MAX_WEIGHT, float(weight)))


def _read_weights() -> Dict[str, float]:
    """Best-effort read of the usefulness store. Empty dict on any
    missing/corrupt file or malformed entry -- this is a soft cache, not a
    source of truth, so it must never break the caller."""
    path = _usefulness_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    weights = data.get("weights")
    if not isinstance(weights, dict):
        return {}
    out: Dict[str, float] = {}
    for h, w in weights.items():
        try:
            out[str(h)] = _clamp(float(w))
        except (TypeError, ValueError):
            continue
    return out


def _write_weights(weights: Dict[str, float]) -> None:
    path = _usefulness_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".usefulness_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {"weights": weights, "updated_at": datetime.now(timezone.utc).isoformat()},
                f, indent=2, ensure_ascii=False,
            )
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_weight(entry_hash: str) -> float:
    """Return the usefulness weight for an entry hash, defaulting to
    ``DEFAULT_WEIGHT`` when untracked. Never raises."""
    if not entry_hash:
        return DEFAULT_WEIGHT
    try:
        return _read_weights().get(entry_hash, DEFAULT_WEIGHT)
    except Exception:
        logger.debug("retrieval: get_weight failed", exc_info=True)
        return DEFAULT_WEIGHT


def all_weights() -> Dict[str, float]:
    """Return the full ``entry_hash -> weight`` map. Never raises; empty
    dict on any failure."""
    try:
        return _read_weights()
    except Exception:
        logger.debug("retrieval: all_weights failed", exc_info=True)
        return {}


def nudge(entry_hash: str, delta: float) -> float:
    """Adjust one entry's usefulness weight by ``delta``, clamped to
    ``[MIN_WEIGHT, MAX_WEIGHT]``. Guarded/never raises -- returns the
    resulting weight, or ``DEFAULT_WEIGHT`` if the nudge could not be
    applied (missing hash, I/O failure, etc). This NEVER removes an entry
    from the store; it only ever adjusts a soft ranking weight.
    """
    if not entry_hash:
        return DEFAULT_WEIGHT
    try:
        # Reuse MemoryStore's file-lock helper (same lock-file-per-path
        # pattern already used for MEMORY.md/USER.md and the surfaced/
        # archive sidecars) so a concurrent nudge from another session
        # can't race a read-modify-write. Imported lazily to avoid a
        # module-load-time circular import with tools.memory_tool (which
        # imports this module lazily too -- see the module docstring).
        from tools.memory_tool import MemoryStore

        path = _usefulness_path()
        with MemoryStore._file_lock(path):
            weights = _read_weights()
            current = weights.get(entry_hash, DEFAULT_WEIGHT)
            new_weight = _clamp(current + float(delta))
            weights[entry_hash] = new_weight
            _write_weights(weights)
        logger.debug(
            "memory usefulness updated delta=%.3f previous=%.3f current=%.3f",
            float(delta),
            current,
            new_weight,
        )
        return new_weight
    except Exception:
        logger.debug("retrieval: nudge failed for %s", entry_hash, exc_info=True)
        return DEFAULT_WEIGHT


# ---------------------------------------------------------------------------
# Retrieval ranking -- the hook MemoryStore calls at render time.
# ---------------------------------------------------------------------------


def rank_entries(
    entries: List[str],
    *,
    char_limit: int,
    config: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Order (and, only if the budget forces it, select a subset of)
    ``entries`` for prompt injection.

    When ``memory.retrieval.adaptive`` is disabled (default config keeps it
    enabled, but callers/tests can flip it off), or on ANY internal
    failure, this returns ``entries`` completely unchanged -- byte-for-byte
    identical to pre-Loop-4 behavior. This is what the golden test in
    ``tests/agent/memory/test_retrieval.py`` pins down.

    When enabled: entries are stable-sorted by usefulness weight
    (descending). Python's sort is stable, so entries with EQUAL weight
    keep their original relative order -- this is the "in ADDITION to the
    existing recency/order" the spec asks for; recency/insertion order is
    the tiebreaker, not replaced. The sorted entries are then greedily
    packed into ``char_limit`` (joined the same way the § entries are
    joined for the actual prompt block); an entry that doesn't fit is left
    out of THIS render only -- it is never removed from the live store, and
    it competes again on the next render. If NOTHING fits (a pathological
    char_limit smaller than the shortest entry), falls back to the
    original list rather than rendering an empty block.
    """
    if not entries:
        return []
    try:
        cfg = config if config is not None else retrieval_config()
        if not cfg.get("adaptive", True):
            return list(entries)

        from tools.memory_tool import entry_hash

        weighted = [(get_weight(entry_hash(e)), e) for e in entries]
        # Stable sort: ties preserve original (recency/insertion) order.
        weighted.sort(key=lambda pair: -pair[0])

        selected: List[str] = []
        total = 0
        for _weight, e in weighted:
            addition = len(e) if not selected else len(_ENTRY_DELIMITER) + len(e)
            if total + addition <= char_limit:
                selected.append(e)
                total += addition

        result = selected if selected else list(entries)
        logger.debug(
            "memory retrieval ranked entries=%d selected=%d reordered=%s char_limit=%d",
            len(entries),
            len(result),
            result != list(entries),
            char_limit,
        )
        return result
    except Exception:
        logger.debug("retrieval: rank_entries failed, using original order", exc_info=True)
        return list(entries)


# ---------------------------------------------------------------------------
# Injection -> outcome capture (see module docstring for the linkage design)
# ---------------------------------------------------------------------------


def capture_previous_batch_outcome() -> None:
    """Score the PREVIOUS session's surfaced memory batch against outcomes
    recorded since then, nudging those entries' usefulness up or down.

    Called once per ``MemoryStore`` instance from
    ``format_for_system_prompt()``, before it stamps THIS session's fresh
    batch into ``memory/surfaced.json`` -- so "previous batch" means the
    entries sharing the newest ``last_surfaced`` timestamp already on disk
    at that moment. Best-effort, NON-BLOCKING, and NEVER raises: any
    failure is logged at debug level and swallowed. A disabled
    ``memory.retrieval.adaptive`` config makes this a no-op.

    See the module docstring for why this is per-SESSION rather than
    per-turn, and why that's an honest, documented approximation rather
    than the ideal per-turn linkage.
    """
    try:
        cfg = retrieval_config()
        if not cfg.get("adaptive", True):
            return

        from tools.memory_tool import get_all_surfaced

        table = get_all_surfaced()
        if not table:
            return

        timestamps = [row.get("last_surfaced") for row in table.values() if row.get("last_surfaced")]
        if not timestamps:
            return
        prev_ts = max(timestamps)
        prev_hashes = [h for h, row in table.items() if row.get("last_surfaced") == prev_ts]
        if not prev_hashes:
            return

        corrected = _correction_since(prev_ts)
        delta = -cfg["learning_rate"] if corrected else cfg["learning_rate"]
        for h in prev_hashes:
            nudge(h, delta)
        logger.info(
            "memory usefulness batch scored entries=%d corrected=%s delta=%.3f",
            len(prev_hashes),
            corrected,
            delta,
        )
    except Exception:
        logger.debug("retrieval: capture_previous_batch_outcome failed", exc_info=True)


def _correction_since(since_iso: str) -> bool:
    """True if an escalation-loop correction was recorded at/after
    ``since_iso``. Reuses the EXISTING outcome ledger
    (``agent.learning.outcomes``) and the EXISTING correction signal --
    adds no new detection logic. Never raises."""
    try:
        from agent.learning.outcomes import recent

        rows = recent(loop="escalation", since=since_iso, limit=50)
        return any(row.get("event") == "corrected" for row in rows)
    except Exception:
        logger.debug("retrieval: _correction_since failed", exc_info=True)
        return False
