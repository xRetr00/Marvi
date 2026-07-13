"""Flow gate -- holds proactive/cron-originated deliveries while the user
is heads-down in a focus app (IDE/editor/terminal), flushing when they go
afk or switch away.

Wired into ``gateway/delivery.py`` with the smallest possible edit: a
single ``await wait_if_gated(metadata)`` at the top of
``DeliveryRouter._deliver_to_platform``.

Why that call site is the right (and only) gate point: ``_deliver_to_platform``
is exclusively reached from ``DeliveryRouter.deliver()`` (cron job output
delivery) and directly from ``cron/scheduler.py``'s live-adapter send path
for the running gateway loop -- both are cron/scheduled delivery, and both
stamp ``"job_id"`` into ``metadata``. Live conversational replies are sent
by the platform adapter directly inside ``gateway/run.py``'s message
handler and never call ``DeliveryRouter`` at all, so gating here is
naturally a no-op for direct replies without needing a separate origin
flag threaded through the whole send path.

No-op (delivers immediately) unless ALL of:
  - the delivery is cron/proactive-originated (``metadata["job_id"]`` set),
  - ``presence.enabled`` and ``presence.flow_gating`` are both true,
  - ActivityWatch is reachable, and
  - the user is currently not-afk with a focus-app (IDE/editor/terminal)
    window in the foreground.

Never loses a message: while held, delivery resumes on whichever comes
first --
  - the user going afk or switching away from the focus app (checked on a
    bounded poll interval),
  - a fail-open max-hold ceiling (:data:`MAX_HOLD_SECONDS`), or
  - process shutdown -- an ``atexit`` hook flips a module-level "flush now"
    flag that every currently-waiting poll observes on its next tick, so a
    held message is delivered rather than dropped when the gateway exits.

Deliberately simple (in-memory only, per the design spec): a held item is
just a live coroutine parked in ``asyncio.sleep``, not a persisted queue.
If the process is killed (not a graceful shutdown), the held delivery is
lost -- exactly as it would be for any other in-flight async gateway work.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# How often a held delivery re-checks whether it's safe to send.
POLL_INTERVAL_SECONDS = 20.0

# Fail-open ceiling. A message is never held longer than this even if the
# user never leaves the focus app -- worst case is one interruption during
# an unusually long uninterrupted focus session, which beats a message
# silently never arriving.
MAX_HOLD_SECONDS = 15 * 60

# Flipped by the atexit hook below; every waiting poll checks this each
# iteration so a held delivery flushes immediately on process shutdown
# instead of being lost.
_shutdown_event = threading.Event()


def _flush_all_on_shutdown() -> None:
    _shutdown_event.set()


atexit.register(_flush_all_on_shutdown)


def is_cron_origin(metadata: Optional[Dict[str, Any]]) -> bool:
    """True when ``metadata`` marks this as a cron/scheduler-originated send.

    See the module docstring for why ``job_id`` presence is the right
    signal here (every caller of ``_deliver_to_platform`` is cron/proactive
    delivery already; this keeps the gate explicit and self-documenting
    rather than relying on "nothing else calls this class").
    """
    return bool(metadata and metadata.get("job_id"))


def _focus_app_active() -> bool:
    """True iff the user is currently not-afk with a focus app (IDE/editor/
    terminal) in the foreground. False -- never gate -- on any error or
    when ActivityWatch is unreachable."""
    try:
        from tools.presence.aw_client import aw_client
        from tools.presence.common import is_focus_app

        if not aw_client.is_available():
            return False
        if aw_client.get_afk_state() == "afk":
            return False
        window = aw_client.get_current_window()
        if not window:
            return False
        app = (window.get("data") or {}).get("app")
        return is_focus_app(app)
    except Exception:
        logger.debug("flow_gate: focus-app probe failed", exc_info=True)
        return False


def should_gate(metadata: Optional[Dict[str, Any]]) -> bool:
    """One-shot synchronous decision: should this delivery be held right now?"""
    if not is_cron_origin(metadata):
        return False
    try:
        from tools.presence.common import get_presence_config

        cfg = get_presence_config()
    except Exception:
        logger.debug("flow_gate: config read failed; not gating", exc_info=True)
        return False
    if not cfg.get("enabled") or not cfg.get("flow_gating"):
        return False
    # Rhythm-model stand-down: when the user's own 14-day activity history
    # says this hour is outside their typical active window for today, they
    # aren't in deep work by definition -- deliver freely without gating.
    # Guarded import + fail-back-to-old-behavior: no rhythm file, no data
    # for today's weekday, or any error makes this check a no-op.
    try:
        from tools.presence.rhythm import is_outside_active_hours

        if is_outside_active_hours():
            return False
    except Exception:
        logger.debug("flow_gate: rhythm check failed; ignoring", exc_info=True)
    return _focus_app_active()


async def wait_if_gated(metadata: Optional[Dict[str, Any]]) -> None:
    """Block until it's safe to deliver, or give up (fail-open) after
    :data:`MAX_HOLD_SECONDS` / on shutdown.

    Returns immediately (no-op) unless :func:`should_gate` is True at call
    time. Safe to call unconditionally from every ``_deliver_to_platform``
    invocation -- it is the single hook point delivery.py needs.
    """
    # should_gate() (and the _focus_app_active() checks below) do synchronous
    # network I/O (tools.presence.aw_client's requests-based HTTP client, up
    # to DEFAULT_TIMEOUT_SECONDS per call) and, on Win32 fallback paths,
    # blocking ctypes calls. Run them off the event loop via asyncio.to_thread
    # so a slow/unreachable ActivityWatch server stalls only this coroutine,
    # not every other in-flight request/delivery on the gateway's loop.
    if not await asyncio.to_thread(should_gate, metadata):
        return

    job_id = (metadata or {}).get("job_id")
    logger.info("flow_gate: holding delivery (job_id=%s) -- user active in a focus app", job_id)

    deadline = time.monotonic() + MAX_HOLD_SECONDS
    while time.monotonic() < deadline:
        if _shutdown_event.is_set():
            logger.info("flow_gate: shutdown in progress -- flushing held delivery (job_id=%s)", job_id)
            return
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))
        if _shutdown_event.is_set():
            logger.info("flow_gate: shutdown in progress -- flushing held delivery (job_id=%s)", job_id)
            return
        if not await asyncio.to_thread(_focus_app_active):
            logger.info("flow_gate: user left focus app -- flushing held delivery (job_id=%s)", job_id)
            return

    logger.info("flow_gate: max hold (%ss) reached -- flushing held delivery (job_id=%s)",
                MAX_HOLD_SECONDS, job_id)
