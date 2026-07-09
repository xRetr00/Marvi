"""Idle trigger — fires one subconscious tick after N minutes of user silence.

Workstream A's gateway hook for Contract 3's
``subconscious.idle_trigger_minutes``. Reuses the gateway's existing
"last inbound seen" clock (``HermesGateway._last_inbound_at``, stamped at
the single inbound chokepoint in ``_handle_message`` for every real,
non-internal message — see gateway/run.py, added for scale-to-zero)
instead of adding a second observation point. Debounced so sustained
silence fires the tick exactly once per idle window: a fresh inbound
message resets the window and re-arms the trigger for the next one.

No new engine: firing the trigger just calls
``cron.subconscious.trigger_tick``, which nudges the SAME built-in cron job
the ``hermes subconscious`` CLI manages (``cron.jobs.trigger_job`` sets
``next_run_at`` to now; the existing 60s ticker picks it up on its next
loop iteration).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# How often the watcher polls. Coarser than the debounce window itself
# (idle_trigger_minutes, default 15m) so this is cheap; fine-grained
# precision on WHEN within a minute the trigger fires doesn't matter.
DEFAULT_WATCH_INTERVAL_SECONDS = 60.0


def should_fire(
    *,
    seconds_since_last_inbound: float,
    idle_trigger_minutes: int,
    last_inbound_at: float,
    last_fired_for_inbound_at: Optional[float],
) -> bool:
    """Pure debounce predicate — testable without a live gateway.

    Fires once per idle window: True only when the silence threshold has
    been crossed AND we have not already fired for this same inbound
    timestamp. A fresh inbound message advances ``last_inbound_at``, which
    re-arms the trigger for the NEXT idle window — so sustained silence
    never double-fires, but silence-then-message-then-silence-again fires
    twice, once per window.

    ``idle_trigger_minutes <= 0`` disables the trigger entirely (explicit
    opt-out, matches Contract 3's config semantics for a non-positive value).
    """
    if idle_trigger_minutes <= 0:
        return False
    if seconds_since_last_inbound < idle_trigger_minutes * 60.0:
        return False
    if last_fired_for_inbound_at is not None and last_fired_for_inbound_at >= last_inbound_at:
        return False
    return True


async def watch(gateway, *, interval: float = DEFAULT_WATCH_INTERVAL_SECONDS) -> None:
    """Background watcher: poll for idle silence and fire the subconscious tick.

    Started as an ``asyncio.create_task`` from ``gateway/run.py`` alongside
    the other best-effort background watchers (scale-to-zero, kanban
    dispatcher, async-delegation, ...). Never raises out of the loop — any
    per-iteration failure is logged and the watcher keeps running. No-ops
    (stays in the loop without firing) whenever ``subconscious.enabled`` is
    false, so an unconfigured instance behaves exactly as before this
    feature existed.
    """
    last_fired_for_inbound_at: Optional[float] = None
    await asyncio.sleep(min(interval, 30.0))  # let startup settle
    while getattr(gateway, "_running", True):
        try:
            await asyncio.sleep(interval)
            if not getattr(gateway, "_running", True):
                return

            from cron.subconscious import (
                idle_trigger_minutes as _cfg_idle_trigger_minutes,
                is_enabled,
                trigger_tick,
            )

            if not is_enabled():
                continue
            last_inbound_at = getattr(gateway, "_last_inbound_at", None)
            if last_inbound_at is None:
                continue
            minutes = _cfg_idle_trigger_minutes()
            fire = should_fire(
                seconds_since_last_inbound=time.time() - last_inbound_at,
                idle_trigger_minutes=minutes,
                last_inbound_at=last_inbound_at,
                last_fired_for_inbound_at=last_fired_for_inbound_at,
            )
            if not fire:
                continue
            if trigger_tick(reason="idle"):
                last_fired_for_inbound_at = last_inbound_at
                logger.info(
                    "idle-trigger: fired subconscious tick after %.0fm of silence",
                    minutes,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the watcher must never crash the gateway
            logger.debug("idle-trigger watcher iteration error", exc_info=True)
