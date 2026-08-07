"""Suggested cron jobs — proposed automations the user accepts with one tap.

A *suggestion* is a ready-to-run cron job spec that Hermes surfaces to the
user, who accepts it (creates the real cron job) or dismisses it (latched so
it is never re-offered). This is the single surface every automation proposal
flows through, regardless of where it came from:

  * ``catalog``  — a curated starter automation (daily briefing, important-mail
                   monitor, weekly digest, ...).
  * ``blueprint``   — the user installed a skill that carries a ``blueprint:`` block
                   (see ``tools/blueprints.py``); installing it registers a
                   suggestion instead of auto-scheduling.
  * ``usage``    — the background self-improvement review noticed a recurring
                   ask that a scheduled job would serve.
  * ``integration`` — the user connected an account (Gmail, GitHub, ...) and
                   the obvious automations for that surface are offered.
  * ``subconscious`` — the subconscious tick (``cron/subconscious.py``)
                   noticed something during its diff+goals+memory reasoning
                   pass that's worth proposing as an automation instead of
                   interrupting the user (Contract 2, design spec
                   2026-07-09-marvi-subconscious-presence).

Accepting a job suggestion calls the existing ``cron.jobs.create_job`` with
the stored ``job_spec``; goal and guarded learned-config suggestions use their
existing stores. There is NO second job engine. Suggestions never auto-create
jobs or apply learned config; acceptance is always explicit (consent-first). Dismissed
suggestions latch by a stable ``dedup_key`` so the same proposal is not
re-offered after the user says no.

Every suggestion also carries a ``category`` (default ``"general"``) used to
look up a *proactivity tier* — ``notify`` (surface only), ``propose``
(one-tap accept, the default), or ``auto`` (pre-approved categories may be
acted on without the user tapping accept). Tiers are user-configured per
category in ``subconscious.tiers`` (config.yaml, Contract 3); see
``resolve_tier`` / ``is_auto_tier``. The tier system does not change how THIS
module behaves — even an ``auto`` category still goes through
``add_suggestion`` unless the caller explicitly checks ``is_auto_tier`` and
chooses to act immediately instead. Consent-first stays the default in every
case where the caller doesn't opt into acting on ``auto`` itself.

Storage mirrors ``cron/jobs.py``: ``~/.hermes/cron/suggestions.json``, atomic
writes, an in-process lock, and 0600 perms.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from utils import atomic_replace

logger = logging.getLogger(__name__)

# Per-profile by design (issue #4707): suggestions live alongside the active
# profile's cron store. Anchor on get_hermes_home() (profile home), not the
# shared default root. See cron/jobs.py for the full rationale.
CRON_DIR = get_hermes_home().resolve() / "cron"
SUGGESTIONS_FILE = CRON_DIR / "suggestions.json"

# In-process lock protecting load->modify->save cycles (the background review
# fork and the main agent can both write).
_suggestions_lock = threading.Lock()

# Cap pending suggestions so the list never becomes a nag wall. When full,
# new suggestions are dropped (the user should clear the backlog first).
MAX_PENDING = 5

VALID_SOURCES = frozenset({"catalog", "blueprint", "usage", "integration", "subconscious"})
_STATUS_PENDING = "pending"
_STATUS_ACCEPTED = "accepted"
_STATUS_DISMISSED = "dismissed"

# Proactivity tiers (Contract 2). "notify" and "auto" are reserved for future
# delivery-path wiring (D/UI); this module only distinguishes "auto" so
# callers can decide whether a pre-approved category may act without an
# explicit accept tap. Everything not configured defaults to "propose" —
# the safe, consent-first default.
VALID_TIERS = frozenset({"notify", "propose", "auto"})
DEFAULT_TIER = "propose"
DEFAULT_CATEGORY = "general"


def _secure_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ensure_dir() -> None:
    CRON_DIR.mkdir(parents=True, exist_ok=True)


def _load_raw() -> Dict[str, Any]:
    if not SUGGESTIONS_FILE.exists():
        return {"suggestions": []}
    try:
        with open(SUGGESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("suggestions.json unreadable (%s); starting empty", e)
        return {"suggestions": []}
    if isinstance(data, dict) and isinstance(data.get("suggestions"), list):
        return data
    if isinstance(data, list):
        return {"suggestions": data}
    logger.warning("suggestions.json malformed; starting empty")
    return {"suggestions": []}


def _save_raw(suggestions: List[Dict[str, Any]]) -> None:
    _ensure_dir()
    fd, tmp_path = tempfile.mkstemp(dir=str(SUGGESTIONS_FILE.parent), suffix=".tmp", prefix=".sugg_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {"suggestions": suggestions, "updated_at": _hermes_now().isoformat()},
                f,
                indent=2,
            )
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, SUGGESTIONS_FILE)
        _secure_file(SUGGESTIONS_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_suggestions() -> List[Dict[str, Any]]:
    """Return all suggestion records (any status)."""
    return _load_raw().get("suggestions", [])


def list_pending() -> List[Dict[str, Any]]:
    """Return pending suggestions in creation order (oldest first)."""
    return [s for s in load_suggestions() if s.get("status") == _STATUS_PENDING]


def list_suggestions_created_after(
    since_iso: str, *, source: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Return suggestions (any status) created at or after ``since_iso``.

    Used by the subconscious-tick activity log (``cron/scheduler.py``) to
    classify a tick's outcome as ``"suggestion"`` when the run registered a
    new automation proposal via ``suggest_automation`` rather than staying
    silent. Best-effort: a malformed/missing ``since_iso`` or per-record
    ``created_at`` is skipped defensively rather than raising — this is a
    classification helper, not a source of truth for the suggestion store
    itself.
    """
    try:
        since_dt = datetime.fromisoformat(since_iso)
    except (ValueError, TypeError):
        return []

    matches: List[Dict[str, Any]] = []
    for s in load_suggestions():
        if source is not None and s.get("source") != source:
            continue
        created_at = s.get("created_at")
        if not created_at:
            continue
        try:
            created_dt = datetime.fromisoformat(created_at)
        except (ValueError, TypeError):
            continue
        if created_dt >= since_dt:
            matches.append(s)
    return matches


def add_suggestion(
    *,
    title: str,
    description: str,
    source: str,
    job_spec: Optional[Dict[str, Any]] = None,
    kind: str = "job",
    goal_spec: Optional[Dict[str, Any]] = None,
    config_spec: Optional[Dict[str, Any]] = None,
    memory_spec: Optional[Dict[str, Any]] = None,
    loop: Optional[str] = None,
    dedup_key: str,
    category: str = DEFAULT_CATEGORY,
) -> Optional[Dict[str, Any]]:
    """Register a pending suggestion. Returns the record, or None if skipped.

    Skipped when: the source is unknown, the same ``dedup_key`` was already
    dismissed or accepted (never re-offer), an identical pending suggestion
    exists, or the pending list is full (``MAX_PENDING``).

    ``job_spec`` is a dict of kwargs for ``cron.jobs.create_job`` — accepting
    the suggestion passes it straight through, so there is no second schema to
    keep in sync.

    ``config_spec`` is normalized against ``agent.learning.registry`` before
    it reaches disk. Acceptance repeats validation and uses a stale-current
    compare-and-set guard before writing config.yaml.

    ``memory_spec`` (kind="memory") carries a minimal review-only payload for
    the memory-decay pass (``agent/memory/decay.py``, Loop 3):
    ``{"op": "merge"|"contradiction", "target": "memory"|"user", ...}``.
    A "merge" suggestion's acceptance performs the merge (archives
    ``drop_text``, keeping ``keep_text``); a "contradiction" suggestion is
    acknowledgment-only on accept — decay never auto-resolves a conflict, so
    there is no destructive action to take even when the user taps Accept.

    ``category`` (default ``"general"``) is the key used to look up the
    user's proactivity tier for this proposal (``resolve_tier``); it is
    stored on the record purely for display/filtering — this call always
    creates a *pending* suggestion regardless of the resolved tier.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"unknown suggestion source: {source!r}")
    if kind not in {"job", "goal", "config", "memory"}:
        raise ValueError("kind must be 'job', 'goal', 'config', or 'memory'")
    if kind == "job" and not isinstance(job_spec, dict):
        raise ValueError("job_spec is required for job suggestions")
    if kind == "goal" and not isinstance(goal_spec, dict):
        raise ValueError("goal_spec is required for goal suggestions")
    if kind == "memory":
        if not isinstance(memory_spec, dict) or memory_spec.get("op") not in {"merge", "contradiction"}:
            raise ValueError("memory_spec (with op='merge' or 'contradiction') is required for memory suggestions")
    if kind == "config":
        from agent.learning.config_registry import validate_config_spec

        config_spec = validate_config_spec(config_spec or {})
    if loop is not None:
        from agent.learning.outcomes import VALID_LOOPS

        if loop not in VALID_LOOPS:
            raise ValueError(f"unknown learning loop: {loop!r}")
    if not title.strip() or not dedup_key.strip():
        raise ValueError("title and dedup_key are required")
    category = (category or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY

    with _suggestions_lock:
        suggestions = _load_raw().get("suggestions", [])

        # Never re-offer something the user already saw and decided on, and
        # never duplicate a still-pending proposal.
        for existing in suggestions:
            if existing.get("dedup_key") == dedup_key:
                if existing.get("status") in (_STATUS_DISMISSED, _STATUS_ACCEPTED):
                    return None
                if existing.get("status") == _STATUS_PENDING:
                    return None

        pending_count = sum(1 for s in suggestions if s.get("status") == _STATUS_PENDING)
        if pending_count >= MAX_PENDING:
            logger.info("Suggestion backlog full (%d); dropping %r", MAX_PENDING, title)
            return None

        record = {
            "id": uuid.uuid4().hex[:12],
            "title": title.strip(),
            "description": description.strip(),
            "source": source,
            "category": category,
            "kind": kind,
            "job_spec": job_spec,
            "goal_spec": goal_spec,
            "config_spec": config_spec,
            "memory_spec": memory_spec,
            "loop": loop,
            "dedup_key": dedup_key.strip(),
            "status": _STATUS_PENDING,
            "created_at": _hermes_now().isoformat(),
        }
        suggestions.append(record)
        _save_raw(suggestions)
        return record


def get_suggestion(ref: str) -> Optional[Dict[str, Any]]:
    """Resolve a suggestion by id, 1-based pending index, or title (exact)."""
    suggestions = load_suggestions()
    # By id.
    for s in suggestions:
        if s.get("id") == ref:
            return s
    # By 1-based pending index.
    if ref.isdigit():
        pending = [s for s in suggestions if s.get("status") == _STATUS_PENDING]
        idx = int(ref) - 1
        if 0 <= idx < len(pending):
            return pending[idx]
    # By exact title (case-insensitive).
    for s in suggestions:
        if s.get("title", "").lower() == ref.lower():
            return s
    return None


def _set_status(suggestion_id: str, status: str) -> bool:
    with _suggestions_lock:
        suggestions = _load_raw().get("suggestions", [])
        changed = False
        for s in suggestions:
            if s.get("id") == suggestion_id:
                s["status"] = status
                s["resolved_at"] = _hermes_now().isoformat()
                changed = True
                break
        if changed:
            _save_raw(suggestions)
        return changed


def dismiss_suggestion(ref: str) -> bool:
    """Dismiss a suggestion (latched — never re-offered for its dedup_key)."""
    s = get_suggestion(ref)
    if not s or s.get("status") != _STATUS_PENDING:
        return False
    changed = _set_status(s["id"], _STATUS_DISMISSED)
    if changed:
        _record_suggestion_outcome(s, "dismissed", accepted_by="user")
    return changed


def _record_suggestion_outcome(suggestion: Dict[str, Any], event: str, *, accepted_by: str) -> None:
    """Best-effort feedback hook; the suggestion action has already succeeded."""
    try:
        from agent.learning.outcomes import record

        loop = suggestion.get("loop") or "trust"
        outcome_event = event if accepted_by == "user" else "observed"
        detail = {
            "suggestion_id": suggestion.get("id"),
            "kind": suggestion.get("kind", "job"),
            "title": suggestion.get("title", ""),
            "accepted_by": accepted_by,
        }
        config_spec = suggestion.get("config_spec")
        if isinstance(config_spec, dict):
            detail.update({"path": config_spec.get("path"), "value": config_spec.get("value")})
        record(
            str(loop),
            outcome_event,
            category=str(suggestion.get("category") or DEFAULT_CATEGORY),
            ref=str(suggestion.get("dedup_key") or suggestion.get("id") or ""),
            detail=detail,
        )
    except Exception:
        logger.debug("Could not record suggestion learning outcome", exc_info=True)


def accept_suggestion(
    ref: str,
    *,
    origin: Optional[Dict[str, Any]] = None,
    accepted_by: str = "user",
) -> Optional[Dict[str, Any]]:
    """Accept a pending job, goal, or guarded config suggestion.

    Returns the created object/change dict, or None if the suggestion isn't found /
    not pending. A job_spec is passed straight to ``cron.jobs.create_job``;
    an ``origin`` (platform/chat) is merged so "origin" delivery routes back to
    the chat where the user accepted.
    """
    s = get_suggestion(ref)
    if not s or s.get("status") != _STATUS_PENDING:
        return None

    kind = s.get("kind", "job")
    if kind == "config":
        from agent.learning.config_registry import apply_config_spec

        result = apply_config_spec(dict(s.get("config_spec") or {}))
        _set_status(s["id"], _STATUS_ACCEPTED)
        _record_suggestion_outcome(s, "accepted", accepted_by=accepted_by)
        return result

    if kind == "memory":
        spec = dict(s.get("memory_spec") or {})
        op = spec.get("op")
        result: Dict[str, Any] = {"op": op}
        if op == "merge":
            from tools.memory_tool import archive_entry, load_on_disk_store

            target = spec.get("target", "memory")
            drop_text = str(spec.get("drop_text") or "")
            store = load_on_disk_store()
            record = archive_entry(store, target, drop_text, reason="dedup: user-approved merge")
            result["archived"] = record
        # "contradiction" (and any future review-only op): acknowledgment
        # only. Decay never auto-resolves a conflict -- accepting just marks
        # the suggestion seen; the user reconciles the entries themselves
        # (via the memory tool / chat) if/when they choose to.
        _set_status(s["id"], _STATUS_ACCEPTED)
        _record_suggestion_outcome(s, "accepted", accepted_by=accepted_by)
        return result

    if kind == "goal":
        from agent.goal_store import add_goal, update_goal

        spec = dict(s.get("goal_spec") or {})
        action = str(spec.pop("action", "add"))
        if action == "add":
            result = add_goal(
                title=str(spec.get("title") or s.get("title") or ""),
                detail=str(spec.get("detail") or ""),
                horizon=str(spec.get("horizon") or "short"),
            )
        elif action in {"pause", "done"}:
            ref_value = str(spec.get("goal_id") or spec.get("title") or "")
            result = update_goal(ref_value, status="paused" if action == "pause" else "done")
            if result is None:
                return None
        else:
            raise ValueError(f"unknown goal suggestion action: {action!r}")
        _set_status(s["id"], _STATUS_ACCEPTED)
        _record_suggestion_outcome(s, "accepted", accepted_by=accepted_by)
        return result

    from cron.scheduler import (
        CronSchedulerRegistrationError,
        create_job_with_scheduler_registration,
    )

    spec = dict(s.get("job_spec") or {})
    if origin is not None and "origin" not in spec:
        spec["origin"] = origin

    try:
        job = create_job_with_scheduler_registration(**spec)
    except CronSchedulerRegistrationError:
        # The job is already durable. Resolve the suggestion so retrying the
        # same acceptance cannot create another local copy.
        _set_status(s["id"], _STATUS_ACCEPTED)
        raise
    _set_status(s["id"], _STATUS_ACCEPTED)
    _record_suggestion_outcome(s, "accepted", accepted_by=accepted_by)
    return job


def clear_resolved() -> int:
    """Drop accepted/dismissed records from disk. Returns the count removed.

    Pending suggestions and the dedup memory of dismissed ones are the only
    things that matter long-term, but dismissed records must be RETAINED for
    their dedup_key (so they aren't re-offered). This only prunes ACCEPTED
    records, which have served their purpose once the job exists.
    """
    with _suggestions_lock:
        suggestions = _load_raw().get("suggestions", [])
        kept = [s for s in suggestions if s.get("status") != _STATUS_ACCEPTED]
        removed = len(suggestions) - len(kept)
        if removed:
            _save_raw(kept)
        return removed


# ---------------------------------------------------------------------------
# Proactivity tiers (Contract 2)
# ---------------------------------------------------------------------------

def get_tiers_config() -> Dict[str, str]:
    """Return the user-configured ``subconscious.tiers`` category->tier map.

    Best-effort: config errors resolve to ``{}`` (everything falls back to
    ``DEFAULT_TIER``) rather than raising, so a malformed config never blocks
    the suggestion pipeline.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config()
        tiers = cfg_get(cfg, "subconscious", "tiers", default={}) or {}
        if not isinstance(tiers, dict):
            return {}
        return {str(k): str(v) for k, v in tiers.items()}
    except Exception as e:
        logger.debug("get_tiers_config failed (%s); defaulting to no overrides", e)
        return {}


def resolve_tier(category: str, *, tiers: Optional[Dict[str, str]] = None) -> str:
    """Resolve a category to its proactivity tier.

    ``tiers`` is injectable for tests; defaults to ``get_tiers_config()``.
    An unrecognized or unconfigured category resolves to ``DEFAULT_TIER``
    ("propose") — the safe, consent-first default. An invalid configured
    value (typo, wrong type) also falls back to the default rather than
    ever silently granting "auto".
    """
    category = (category or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
    if category == "goal":
        return "propose"
    source = tiers if tiers is not None else get_tiers_config()
    tier = str(source.get(category, DEFAULT_TIER)).strip().lower()
    return tier if tier in VALID_TIERS else DEFAULT_TIER


def is_auto_tier(category: str, *, tiers: Optional[Dict[str, str]] = None) -> bool:
    """True iff ``category`` is configured as a pre-approved "auto" tier."""
    return resolve_tier(category, tiers=tiers) == "auto"
