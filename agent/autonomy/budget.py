"""The autonomy budget — spec §1.1.

A daily budget of self-initiated actions so Marvi's freedom to research,
browse, and ask never becomes runaway cost or spam. Mirrors
``cron/subconscious_initiatives.py``'s storage shape (atomic tempfile+replace
JSON file under ``HERMES_HOME``, local-date reset) and
``agent/memory/decay.py``'s config-reading style (``cfg_get`` with inline
defaults — these are UI-edited keys too, but kept out of ``DEFAULT_CONFIG``
here since the schema for POST /api/autonomy/config validates them directly;
see ``hermes_cli/web_server.py``).

Every public function is guarded: never raises. A budget read/write failure
degrades to "no budget available" (fail closed for spending, fail open for
reads) rather than crashing whatever autonomous action was about to happen.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home
from utils import atomic_replace

logger = logging.getLogger(__name__)

DEFAULT_DAILY_ACTION_BUDGET = 8
DEFAULT_PER_CATEGORY: Dict[str, int] = {"research": 4, "browse": 2, "ask_user": 3}

_BUDGET_FILENAME = "budget.json"
# Serializes read-modify-write cycles within this process. Mirrors the
# in-process lock style used by cron/suggestions.py and
# cron/subconscious_initiatives.py for their own JSON stores.
_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def autonomy_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the ``autonomy`` config section with defaults filled in (spec
    §1.1/§2.6). Never raises; falls back to built-in defaults on any read
    failure.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = config if config is not None else load_config()
        raw_per_category = cfg_get(cfg, "autonomy", "per_category", default=None)
        per_category = dict(DEFAULT_PER_CATEGORY)
        if isinstance(raw_per_category, dict):
            for key, value in raw_per_category.items():
                try:
                    per_category[str(key)] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
        return {
            "enabled": bool(cfg_get(cfg, "autonomy", "enabled", default=True)),
            "daily_action_budget": max(
                0,
                int(
                    cfg_get(
                        cfg, "autonomy", "daily_action_budget",
                        default=DEFAULT_DAILY_ACTION_BUDGET,
                    )
                    or DEFAULT_DAILY_ACTION_BUDGET
                ),
            ),
            "per_category": per_category,
            "ask": {
                "max_per_day": max(
                    0,
                    int(cfg_get(cfg, "autonomy", "ask", "max_per_day", default=3) or 3),
                ),
                "quiet_in_deep_work": bool(
                    cfg_get(cfg, "autonomy", "ask", "quiet_in_deep_work", default=True)
                ),
            },
        }
    except Exception:
        logger.debug("autonomy: config read failed, using defaults", exc_info=True)
        return {
            "enabled": True,
            "daily_action_budget": DEFAULT_DAILY_ACTION_BUDGET,
            "per_category": dict(DEFAULT_PER_CATEGORY),
            "ask": {"max_per_day": 3, "quiet_in_deep_work": True},
        }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def budget_path() -> Path:
    return get_hermes_home() / "autonomy" / _BUDGET_FILENAME


def _empty_state() -> Dict[str, Any]:
    return {"date": date.today().isoformat(), "used_total": 0, "used": {}}


def _load_state_raw() -> Dict[str, Any]:
    path = budget_path()
    if not path.exists():
        return _empty_state()
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    used = data.get("used")
    if not isinstance(used, dict):
        used = {}
    return {
        "date": str(data.get("date") or date.today().isoformat()),
        "used_total": int(data.get("used_total") or 0),
        "used": {str(k): int(v) for k, v in used.items() if isinstance(v, (int, float))},
    }


def _save_state(state: Dict[str, Any]) -> None:
    path = budget_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".budget_", suffix=".tmp")
    try:
        import json

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


def reset_if_new_day(state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return ``state`` (or a freshly loaded one) reset to zero if its
    ``date`` isn't today (local date — mirrors
    ``cron/subconscious_initiatives.py``'s own local-midnight reset).
    Never raises; a state that can't be read is treated as already-reset.
    """
    try:
        current = state if state is not None else _load_state_raw()
        today = date.today().isoformat()
        if current.get("date") != today:
            return _empty_state()
        return current
    except Exception:
        logger.debug("autonomy budget: reset_if_new_day failed", exc_info=True)
        return _empty_state()


def load_state() -> Dict[str, Any]:
    """Load the persisted budget state, reset to zero if it's a new day.
    Never raises."""
    with _lock:
        return reset_if_new_day(_load_state_raw())


# ---------------------------------------------------------------------------
# Spend / query
# ---------------------------------------------------------------------------


def remaining(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return today's budget usage/remaining, per category plus the overall
    daily total. Shape: ``{"date", "enabled", "daily_action_budget",
    "used_total", "remaining_total", "categories": {cat: {"limit","used",
    "remaining"}}}``. Never raises — degrades to a disabled/zeroed snapshot.
    """
    try:
        cfg = autonomy_config(config)
        with _lock:
            state = reset_if_new_day(_load_state_raw())
        used_total = int(state.get("used_total") or 0)
        used_by_cat = state.get("used") or {}
        categories: Dict[str, Any] = {}
        for cat, limit in cfg["per_category"].items():
            used = int(used_by_cat.get(cat) or 0)
            categories[cat] = {
                "limit": limit,
                "used": used,
                "remaining": max(0, limit - used),
            }
        return {
            "date": state.get("date"),
            "enabled": cfg["enabled"],
            "daily_action_budget": cfg["daily_action_budget"],
            "used_total": used_total,
            "remaining_total": max(0, cfg["daily_action_budget"] - used_total),
            "categories": categories,
        }
    except Exception:
        logger.debug("autonomy budget: remaining() failed", exc_info=True)
        return {
            "date": date.today().isoformat(),
            "enabled": False,
            "daily_action_budget": 0,
            "used_total": 0,
            "remaining_total": 0,
            "categories": {},
        }


def _log_spend_activity(category: str, ok: bool, snapshot: Dict[str, Any]) -> None:
    """Best-effort: every autonomy budget spend attempt (successful or not)
    is logged to the shared activity feed with source "autonomy" (spec
    §1.1). Guarded import — cron.scheduler is the module that owns the
    activity.jsonl appender (``record_subconscious_activity``); a circular
    or missing import must never break budget spending itself.
    """
    try:
        from cron.scheduler import record_subconscious_activity

        cat_info = (snapshot.get("categories") or {}).get(category, {})
        summary = (
            f"autonomy: spent 1 '{category}' budget "
            f"({cat_info.get('used', '?')}/{cat_info.get('limit', '?')} used today)"
            if ok
            else f"autonomy: '{category}' budget exhausted for today"
        )
        record_subconscious_activity(
            source="autonomy",
            outcome="message" if ok else "no_change",
            summary=summary,
        )
    except Exception:
        logger.debug("autonomy budget: activity log failed", exc_info=True)


def try_spend(category: str, *, log_activity: bool = True) -> bool:
    """Attempt to spend one unit of the named category's budget.

    Decrements both the category counter and the overall daily total when
    both have room; otherwise leaves state untouched and returns False. An
    unknown category (not in ``autonomy.per_category``) or a disabled
    ``autonomy.enabled`` always fails closed. Never raises.
    """
    category = str(category or "").strip()
    if not category:
        return False
    try:
        cfg = autonomy_config()
        if not cfg["enabled"]:
            return False
        limit = cfg["per_category"].get(category)
        if limit is None:
            logger.debug("autonomy budget: unknown category %r", category)
            return False
        with _lock:
            state = reset_if_new_day(_load_state_raw())
            used_total = int(state.get("used_total") or 0)
            used_by_cat = dict(state.get("used") or {})
            used_cat = int(used_by_cat.get(category) or 0)
            if used_total >= cfg["daily_action_budget"] or used_cat >= limit:
                ok = False
            else:
                used_by_cat[category] = used_cat + 1
                state["used"] = used_by_cat
                state["used_total"] = used_total + 1
                _save_state(state)
                ok = True
        if log_activity:
            _log_spend_activity(category, ok, remaining(cfg))
        return ok
    except Exception:
        logger.debug("autonomy budget: try_spend(%r) failed", category, exc_info=True)
        return False
