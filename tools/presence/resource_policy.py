"""Resource policy -- get out of the user's way when they're running a
heavy foreground app (fullscreen game, video editor, 3D tool).

When :func:`is_heavy_foreground` is true, Marvi should:
  - demote the voice stack to cold (:func:`enforce`, via
    ``tools.voice_residency.demote`` -- guarded import, that module is
    owned by a parallel workstream and may not exist yet), and
  - defer subconscious background work (the idle-trigger watcher and the
    subconscious tick itself both consult :func:`should_defer_background_work`
    before firing -- see gateway/idle_trigger.py and cron/subconscious.py).

Detection is two-pronged:
  1. App-name match against ``presence.heavy_apps`` (config-driven
     substring list, case-insensitive), resolved via the presence AW
     client when reachable, else a direct Win32 foreground-window probe.
  2. A fullscreen-over-a-monitor check (Win32 only -- AW doesn't expose
     window geometry) -- catches games/tools that don't match the name
     list but are still monopolizing a whole monitor.

Every entry point here is fail-safe: any exception, missing dependency,
non-Windows platform, or unreachable AW server resolves to "not busy"
(``False``) rather than raising or blocking Marvi's normal behavior.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

# How often the policy watcher polls (gateway/run.py startup task).
DEFAULT_WATCH_INTERVAL_SECONDS = 60.0

# How long a should_defer_background_work() verdict is cached before
# re-probing -- keeps repeated polls (idle_trigger watcher, subconscious
# tick, the policy watcher itself) cheap.
_VERDICT_TTL_SECONDS = 30.0

# Sane defaults for "heavy foreground app" -- streaming/recording,
# creative/3D tools, and game-engine editors/launchers where an unasked-for
# voice/background pass would compete for CPU/GPU or steal focus. Matched
# as case-insensitive substrings against the resolved app/process name, so
# both the friendly name ("OBS Studio") and the exe ("obs64.exe") match.
DEFAULT_HEAVY_APPS: tuple[str, ...] = (
    "obs",  # OBS Studio (streaming/recording)
    "blender",
    "resolve",  # DaVinci Resolve
    "premiere",  # Adobe Premiere Pro
    "afterfx",  # Adobe After Effects
    "unity",
    "unreal",
    "steam",  # Steam / Big Picture / game overlay host
)

# Module-level verdict cache for should_defer_background_work().
_verdict_cache: Optional[bool] = None
_verdict_cache_at: float = 0.0


# ---------------------------------------------------------------------------
# Config access (mirrors tools/presence/common.py's presence.* pattern)
# ---------------------------------------------------------------------------


def _presence_cfg() -> dict:
    try:
        from tools.presence.common import get_presence_config

        return get_presence_config()
    except Exception:
        logger.debug("resource_policy: config read failed", exc_info=True)
        return {}


def heavy_apps() -> List[str]:
    """Return the effective ``presence.heavy_apps`` list, or the defaults."""
    cfg = _presence_cfg()
    apps = cfg.get("heavy_apps")
    if not isinstance(apps, list) or not apps:
        return list(DEFAULT_HEAVY_APPS)
    cleaned = [str(a) for a in apps if str(a).strip()]
    return cleaned or list(DEFAULT_HEAVY_APPS)


def _resource_policy_enabled() -> bool:
    cfg = _presence_cfg()
    section = cfg.get("resource_policy")
    if not isinstance(section, dict):
        return True
    return bool(section.get("enabled", True))


def _matches_heavy_list(app_name: Optional[str], apps: Optional[Sequence[str]] = None) -> bool:
    if not app_name:
        return False
    candidates = apps if apps is not None else heavy_apps()
    lowered = app_name.lower()
    return any(needle.lower() in lowered for needle in candidates if needle)


# ---------------------------------------------------------------------------
# Foreground app resolution
# ---------------------------------------------------------------------------


def _foreground_app_name() -> Optional[str]:
    """Best-effort current foreground app name.

    Prefers the presence AW client (already-polled window watcher, no
    extra syscalls) when reachable, falling back to a direct Win32
    foreground-window probe. Returns None (never raises) when neither
    source is available.
    """
    try:
        from tools.presence.aw_client import aw_client

        if aw_client.is_available():
            window = aw_client.get_current_window()
            if window:
                app = (window.get("data") or {}).get("app")
                if app:
                    return str(app)
    except Exception:
        logger.debug("resource_policy: AW foreground app probe failed", exc_info=True)

    if sys.platform.startswith("win"):
        try:
            return _win32_foreground_process_name()
        except Exception:
            logger.debug("resource_policy: win32 foreground app probe failed", exc_info=True)
    return None


def _win32_foreground_hwnd() -> int:
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    return user32.GetForegroundWindow()


def _win32_window_is_fullscreen(hwnd: int) -> bool:
    """True when ``hwnd``'s window rect fully covers the monitor it's on."""
    if not hwnd:
        return False

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False

    MONITOR_DEFAULTTONEAREST = 2
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not monitor:
        return False

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False

    mon = info.rcMonitor
    return (
        rect.left <= mon.left
        and rect.top <= mon.top
        and rect.right >= mon.right
        and rect.bottom >= mon.bottom
    )


def _win32_foreground_process_name() -> Optional[str]:
    """Exe basename (e.g. ``"obs64.exe"``) of the foreground window's owning
    process, via GetWindowThreadProcessId + QueryFullProcessImageNameW."""
    import ctypes
    import os
    from ctypes import wintypes

    hwnd = _win32_foreground_hwnd()
    if not hwnd:
        return None

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return os.path.basename(buf.value) or None
    finally:
        kernel32.CloseHandle(handle)


def _is_fullscreen_foreground() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        hwnd = _win32_foreground_hwnd()
        if not hwnd:
            return False
        return _win32_window_is_fullscreen(hwnd)
    except Exception:
        logger.debug("resource_policy: win32 fullscreen probe failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Public verdicts
# ---------------------------------------------------------------------------


def is_heavy_foreground() -> bool:
    """True when the foreground app matches ``presence.heavy_apps`` OR the
    foreground window is fullscreen over a monitor.

    Never raises -- any failure (AW unreachable, non-Windows platform,
    Win32 call error) resolves to False, i.e. fail toward "not busy" so a
    detection problem never gets stuck holding Marvi back.
    """
    try:
        app_name = _foreground_app_name()
        if _matches_heavy_list(app_name):
            return True
        return _is_fullscreen_foreground()
    except Exception:
        logger.debug("resource_policy: is_heavy_foreground failed", exc_info=True)
        return False


def should_defer_background_work(*, force: bool = False) -> bool:
    """``presence.resource_policy.enabled`` AND :func:`is_heavy_foreground`.

    Cached for :data:`_VERDICT_TTL_SECONDS` so frequent callers (the
    idle-trigger watcher, the subconscious tick, this module's own
    :func:`watch` loop) can poll cheaply without each paying an AW/Win32
    round trip. Pass ``force=True`` to bypass the cache.
    """
    global _verdict_cache, _verdict_cache_at

    now = time.monotonic()
    if not force and _verdict_cache is not None and (now - _verdict_cache_at) < _VERDICT_TTL_SECONDS:
        return _verdict_cache

    verdict = False
    try:
        if _resource_policy_enabled():
            verdict = is_heavy_foreground()
    except Exception:
        logger.debug("resource_policy: should_defer_background_work failed", exc_info=True)
        verdict = False

    _verdict_cache = verdict
    _verdict_cache_at = now
    return verdict


def enforce() -> None:
    """If background work should be deferred and the voice stack is hot,
    demote it to cold.

    Guarded import: ``tools/voice_residency.py`` is owned by a parallel
    workstream and may not exist yet while this module is being developed.
    Idempotent by nature of ``demote()`` -- safe to call every watch tick.
    """
    # The media watcher is part of Presence, so the existing 60-second
    # Presence supervisor loop is also its cheapest crash-recovery path.
    try:
        from hermes_cli.presence_cmd import start_watcher, watcher_pid_if_running
        from tools.presence.common import get_presence_config

        if get_presence_config().get("enabled") and watcher_pid_if_running() is None:
            ok, message = start_watcher()
            (logger.info if ok else logger.warning)("resource_policy: %s", message)
    except Exception:
        logger.debug("resource_policy: media watcher supervision failed", exc_info=True)

    if not should_defer_background_work():
        return

    try:
        from tools import voice_residency
    except ImportError:
        logger.debug("resource_policy: voice_residency module not available; skipping demote")
        return

    try:
        if voice_residency.current_tier() == "hot":
            voice_residency.demote("heavy-foreground-app")
    except Exception:
        logger.debug("resource_policy: enforce() failed calling voice_residency", exc_info=True)


# ---------------------------------------------------------------------------
# Background watcher
# ---------------------------------------------------------------------------


async def watch(gateway, *, interval: float = DEFAULT_WATCH_INTERVAL_SECONDS) -> None:
    """Background watcher: poll :func:`enforce` every ``interval`` seconds.

    Started as an ``asyncio.create_task`` from ``gateway/run.py`` alongside
    the other best-effort background watchers (idle-trigger, scale-to-zero,
    ...) -- mirrors ``gateway/idle_trigger.py``'s ``watch()`` shape. Never
    raises out of the loop -- any per-iteration failure is logged and the
    watcher keeps running.
    """
    await asyncio.sleep(min(interval, 30.0))  # let startup settle
    while getattr(gateway, "_running", True):
        try:
            await asyncio.sleep(interval)
            if not getattr(gateway, "_running", True):
                return
            # enforce() -> is_heavy_foreground() -> _foreground_app_name()
            # does synchronous ActivityWatch HTTP probes (requests, up to
            # DEFAULT_TIMEOUT_SECONDS) plus Win32 ctypes calls. Every 60s
            # tick would otherwise block the gateway's single event loop --
            # and therefore every in-flight message/delivery -- for the
            # length of that probe. Off-load to a worker thread.
            await asyncio.to_thread(enforce)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the watcher must never crash the gateway
            logger.debug("resource-policy watcher iteration error", exc_info=True)
