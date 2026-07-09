"""Goal tools — let the agent read/write the standing goal store.

Registers ``goal_add``, ``goal_update``, and ``goal_list`` (toolset
``goals``) against ``agent/goal_store.py``. These are standing, cross-session
objectives — not the per-turn ``/goal`` Ralph loop in ``hermes_cli/goals.py``.
Active goals are injected into every session's system prompt (see
``agent/system_prompt.py``), so the model can both read them passively there
and manage them explicitly via these tools when the user asks to set,
adjust, or retire a goal.

Also registers ``suggest_automation`` (toolset ``subconscious``) — the
mechanism the subconscious tick (``cron/subconscious.py``) uses to propose a
new automation instead of interrupting the user, per Contract 2's
consent-first suggestion surface. Gated to only appear when
``subconscious.enabled`` is true, so normal chat sessions never see it.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from agent.goal_store import (
    VALID_HORIZONS,
    VALID_STATUSES,
    add_goal,
    get_goal,
    list_goals,
    update_goal,
)
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _subconscious_toolset_enabled() -> bool:
    try:
        from cron.subconscious import is_enabled
        return bool(is_enabled())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Handlers — goals
# ---------------------------------------------------------------------------

def _handle_goal_add(args: dict, **kw) -> str:
    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")
    detail = args.get("detail") or ""
    horizon = args.get("horizon") or "short"
    if horizon not in VALID_HORIZONS:
        return tool_error(f"horizon must be one of {sorted(VALID_HORIZONS)}")
    try:
        goal = add_goal(title=str(title), detail=str(detail), horizon=str(horizon))
    except ValueError as e:
        return tool_error(str(e))
    except Exception as e:
        logger.exception("goal_add failed")
        return tool_error(f"goal_add: {e}")
    return _ok(goal=goal)


def _handle_goal_update(args: dict, **kw) -> str:
    ref = args.get("goal_id") or args.get("ref")
    if not ref:
        return tool_error("goal_id is required (id, list index, or exact title)")
    updates: dict = {}
    for field in ("title", "detail", "status", "horizon"):
        if args.get(field) is not None:
            updates[field] = args[field]
    if not updates:
        return tool_error("provide at least one of: title, detail, status, horizon")
    try:
        goal = update_goal(str(ref), **updates)
    except ValueError as e:
        return tool_error(str(e))
    except Exception as e:
        logger.exception("goal_update failed")
        return tool_error(f"goal_update: {e}")
    if goal is None:
        return tool_error(f"no goal found matching {ref!r}")
    return _ok(goal=goal)


def _handle_goal_list(args: dict, **kw) -> str:
    status = args.get("status")
    horizon = args.get("horizon")
    if status is not None and status not in VALID_STATUSES:
        return tool_error(f"status must be one of {sorted(VALID_STATUSES)}")
    if horizon is not None and horizon not in VALID_HORIZONS:
        return tool_error(f"horizon must be one of {sorted(VALID_HORIZONS)}")
    try:
        goals = list_goals(status=status, horizon=horizon)
    except Exception as e:
        logger.exception("goal_list failed")
        return tool_error(f"goal_list: {e}")
    return json.dumps({"ok": True, "goals": goals, "count": len(goals)})


# ---------------------------------------------------------------------------
# Handlers — subconscious suggestion
# ---------------------------------------------------------------------------

def _handle_suggest_automation(args: dict, **kw) -> str:
    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")
    description = args.get("description") or ""
    dedup_key = args.get("dedup_key")
    if not dedup_key or not str(dedup_key).strip():
        return tool_error(
            "dedup_key is required — a stable key so this proposal is never "
            "re-offered after the user dismisses it once (e.g. "
            "'subconscious:weekly-inbox-digest')"
        )
    job_spec = args.get("job_spec")
    if not isinstance(job_spec, dict) or not job_spec:
        return tool_error(
            "job_spec is required — a dict of kwargs for cron.jobs.create_job "
            "(at minimum 'prompt' and 'schedule')"
        )
    category = args.get("category") or "general"
    try:
        from cron.suggestions import accept_suggestion, add_suggestion, is_auto_tier

        record = add_suggestion(
            title=str(title),
            description=str(description),
            source="subconscious",
            job_spec=job_spec,
            dedup_key=str(dedup_key),
            category=str(category),
        )
    except ValueError as e:
        return tool_error(str(e))
    except Exception as e:
        logger.exception("suggest_automation failed")
        return tool_error(f"suggest_automation: {e}")
    if record is None:
        return _ok(registered=False, reason="already pending/dismissed/accepted, or backlog full")

    # "auto" tier (Contract 2): the user pre-approved this category in
    # subconscious.tiers, so acting IS the consented behavior — accept the
    # suggestion immediately, which creates the real cron job through the
    # exact same path a manual accept takes (dedup latching intact). Any
    # failure degrades to the plain pending suggestion rather than erroring.
    if is_auto_tier(str(category)):
        try:
            job = accept_suggestion(record["id"])
        except Exception:
            logger.exception("suggest_automation: auto-tier accept failed; left as pending")
            job = None
        if job is not None:
            return _ok(registered=True, auto_created=True, tier="auto", job=job, suggestion=record)

    return _ok(registered=True, auto_created=False, suggestion=record)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

GOAL_ADD_SCHEMA = {
    "name": "goal_add",
    "description": (
        "Add a standing goal — a cross-session objective the user wants you "
        "to keep in mind. Active goals are shown to you in every session's "
        "system prompt and steer the subconscious tick's proactive "
        "reasoning. Use this when the user states an ongoing objective "
        "('help me ship the Q3 report', 'I'm learning Spanish this year'), "
        "not for one-off tasks that belong in a todo list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short goal title (required)."},
            "detail": {
                "type": "string",
                "description": "Optional longer description — context, success criteria, constraints.",
            },
            "horizon": {
                "type": "string",
                "enum": sorted(VALID_HORIZONS),
                "description": "'short' (days/weeks) or 'long' (months+). Defaults to 'short'.",
            },
        },
        "required": ["title"],
    },
}

GOAL_UPDATE_SCHEMA = {
    "name": "goal_update",
    "description": (
        "Update an existing goal's title, detail, status, or horizon. Set "
        "status='done' when the user says a goal is complete, 'paused' to "
        "stop steering by it without deleting it, or 'active' to resume."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal_id": {
                "type": "string",
                "description": "Goal id, 1-based list index, or exact title (from goal_list).",
            },
            "title": {"type": "string", "description": "New title."},
            "detail": {"type": "string", "description": "New detail text."},
            "status": {
                "type": "string",
                "enum": sorted(VALID_STATUSES),
                "description": "New status.",
            },
            "horizon": {
                "type": "string",
                "enum": sorted(VALID_HORIZONS),
                "description": "New horizon.",
            },
        },
        "required": ["goal_id"],
    },
}

GOAL_LIST_SCHEMA = {
    "name": "goal_list",
    "description": "List standing goals, optionally filtered by status and/or horizon.",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": sorted(VALID_STATUSES),
                "description": "Optional status filter.",
            },
            "horizon": {
                "type": "string",
                "enum": sorted(VALID_HORIZONS),
                "description": "Optional horizon filter.",
            },
        },
        "required": [],
    },
}

SUGGEST_AUTOMATION_SCHEMA = {
    "name": "suggest_automation",
    "description": (
        "Propose a new automation — the consent-first path (Contract 2). "
        "Only available during the subconscious tick. Registers a pending "
        "suggestion the user accepts with one tap (via /suggestions). The "
        "one exception: when the user has pre-approved the suggestion's "
        "category as an 'auto' tier in subconscious.tiers, the job is "
        "created immediately on their standing consent (the response says "
        "auto_created=true). Always use this tool for new automations; "
        "never try to schedule jobs any other way."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short, human-readable title."},
            "description": {
                "type": "string",
                "description": "1-2 sentence explanation of what it does and why you're proposing it now.",
            },
            "category": {
                "type": "string",
                "description": (
                    "Category key used to look up the user's proactivity tier "
                    "(subconscious.tiers in config.yaml). Defaults to 'general'."
                ),
            },
            "job_spec": {
                "type": "object",
                "description": (
                    "kwargs for cron.jobs.create_job describing the proposed "
                    "job — at minimum 'prompt' and 'schedule'."
                ),
            },
            "dedup_key": {
                "type": "string",
                "description": (
                    "Stable key so this exact proposal is never re-offered "
                    "once accepted or dismissed, e.g. "
                    "'subconscious:weekly-inbox-digest'."
                ),
            },
        },
        "required": ["title", "job_spec", "dedup_key"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="goal_add",
    toolset="goals",
    schema=GOAL_ADD_SCHEMA,
    handler=_handle_goal_add,
    emoji="🎯",
)

registry.register(
    name="goal_update",
    toolset="goals",
    schema=GOAL_UPDATE_SCHEMA,
    handler=_handle_goal_update,
    emoji="🎯",
)

registry.register(
    name="goal_list",
    toolset="goals",
    schema=GOAL_LIST_SCHEMA,
    handler=_handle_goal_list,
    emoji="🎯",
)

registry.register(
    name="suggest_automation",
    toolset="subconscious",
    schema=SUGGEST_AUTOMATION_SCHEMA,
    handler=_handle_suggest_automation,
    check_fn=_subconscious_toolset_enabled,
    emoji="💡",
)
