"""Windows SMTC now-playing watcher -- posts heartbeats to ActivityWatch.

ActivityWatch has no built-in "what's currently playing" watcher, so this
fills that one gap (per the design spec's approved decision: "we build
only aw-watcher-media"). Polls the Windows System Media Transport Controls
(SMTC) session manager every ~5s via the optional `winsdk` package and
posts a heartbeat event into an ``aw-watcher-media_<hostname>`` bucket
(created on first use). Also runs the opt-in goblin shoulder-tap check on
a slower ~5 minute cadence.

`winsdk` is OPTIONAL and Windows-only (see tools/lazy_deps.py's
"presence.media_watcher" entry / the `presence` extra in pyproject.toml).
On any other platform, or when winsdk isn't installed and lazy installs
are declined, this module degrades to a clear one-line message instead of
crashing -- it simply has no now-playing data to contribute.

Run directly:
    python -m tools.presence.media_watcher

Managed by `hermes presence setup` / `pause` / `resume` (see
hermes_cli/presence_cmd.py), which spawns this as a detached background
process and tracks its PID under ~/.hermes/presence/media_watcher.json.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import socket
import sys
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5.0
# AW heartbeat merge window: two heartbeats with identical `data` and a gap
# under this many seconds are merged into one event with extended duration.
PULSETIME_SECONDS = 15.0
GOBLIN_CHECK_INTERVAL_SECONDS = 5 * 60

WINSDK_INSTALL_HINT = (
    "Now-playing tracking needs the optional 'winsdk' package (Windows "
    "only). Install with: pip install \"marvi-agent[presence]\" "
    "(or: pip install winsdk). Presence keeps working without it -- you "
    "just won't get now-playing data."
)

_PLAYBACK_STATUS_NAMES = {
    0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused",
}


def winsdk_available() -> bool:
    """Cheap import-only check -- does not attempt a lazy install."""
    if platform.system() != "Windows":
        return False
    try:
        import winsdk.windows.media.control  # noqa: F401
    except ImportError:
        return False
    return True


def ensure_winsdk(*, prompt: bool = False) -> bool:
    """Attempt to make winsdk importable (lazy-install if needed).

    Returns True on success, False when unsupported/declined/failed --
    never raises. Callers should fall back to "no now-playing data".
    """
    if platform.system() != "Windows":
        return False
    if winsdk_available():
        return True
    try:
        from tools.lazy_deps import ensure, FeatureUnavailable

        try:
            ensure("presence.media_watcher", prompt=prompt)
            return winsdk_available()
        except FeatureUnavailable as exc:
            logger.info("winsdk unavailable: %s", exc)
            return False
    except Exception:
        logger.debug("lazy_deps.ensure failed for presence.media_watcher", exc_info=True)
        return False


def get_current_media() -> Optional[Dict[str, Any]]:
    """Return ``{"app_id", "title", "artist", "status"}`` for the current
    SMTC session, or None when unavailable / nothing is playing."""
    if not winsdk_available():
        return None

    async def _get() -> Optional[Dict[str, Any]]:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        )

        manager = await MediaManager.request_async()
        session = manager.get_current_session()
        if session is None:
            return None
        info = await session.try_get_media_properties_async()
        playback = session.get_playback_info()
        status = "unknown"
        if playback is not None:
            status = _PLAYBACK_STATUS_NAMES.get(int(playback.playback_status), "unknown")
        return {
            "app_id": session.source_app_user_model_id or "",
            "title": (info.title if info else "") or "",
            "artist": (info.artist if info else "") or "",
            "status": status,
        }

    try:
        return asyncio.run(_get())
    except Exception as exc:
        logger.debug("SMTC query failed: %s", exc)
        return None


def media_bucket_id(hostname: Optional[str] = None) -> str:
    return f"aw-watcher-media_{hostname or socket.gethostname()}"


def _maybe_run_goblin_check() -> None:
    try:
        from tools.presence.common import get_presence_config

        cfg = get_presence_config()
        if not cfg.get("goblin", {}).get("shoulder_taps"):
            return
        from tools.presence.goblin import check_stuck_and_notify

        check_stuck_and_notify()
    except Exception:
        logger.debug("goblin shoulder-tap check failed", exc_info=True)


def run_forever() -> int:
    """Poll SMTC and post heartbeats to ActivityWatch until interrupted."""
    if platform.system() != "Windows":
        print("presence media watcher: Windows only, exiting.")
        return 1
    if not ensure_winsdk(prompt=False):
        print(WINSDK_INSTALL_HINT)
        return 1

    from tools.presence.aw_client import AWClient, AWUnavailableError

    client = AWClient()
    bucket_id = media_bucket_id()
    bucket_ready = False
    last_goblin_check = 0.0

    logger.info("presence media watcher starting (bucket=%s)", bucket_id)
    while True:
        try:
            if not client.is_available():
                logger.debug("ActivityWatch not reachable; will retry")
            else:
                if not bucket_ready:
                    try:
                        client.create_bucket(bucket_id, event_type="currently-playing",
                                              client_name="marvi-presence")
                        bucket_ready = True
                    except AWUnavailableError as exc:
                        logger.debug("create_bucket failed: %s", exc)

                if bucket_ready:
                    media = get_current_media()
                    if media and media.get("title"):
                        try:
                            client.heartbeat(
                                bucket_id,
                                {
                                    "app": media["app_id"],
                                    "title": media["title"],
                                    "artist": media["artist"],
                                    "status": media["status"],
                                },
                                pulsetime=PULSETIME_SECONDS,
                            )
                        except AWUnavailableError as exc:
                            logger.debug("heartbeat failed: %s", exc)

            now = time.monotonic()
            if now - last_goblin_check >= GOBLIN_CHECK_INTERVAL_SECONDS:
                last_goblin_check = now
                _maybe_run_goblin_check()
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.exception("presence media watcher: unexpected error in poll loop")

        time.sleep(POLL_INTERVAL_SECONDS)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run_forever()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
