"""Now-playing watcher -- posts heartbeats to ActivityWatch.

ActivityWatch has no built-in "what's currently playing" watcher, so this
fills that one gap (per the design spec's approved decision: "we build
only aw-watcher-media"). Polls the OS media session every ~5s and posts a
heartbeat event into an ``aw-watcher-media_<hostname>`` bucket (created on
first use). Also runs the opt-in goblin shoulder-tap check on a slower
~5 minute cadence.

Cross-platform now-playing adapters, all normalizing to the same event
shape (``{"app_id", "title", "artist", "status"}``):

* **Windows** -- System Media Transport Controls (SMTC) via the optional
  ``winsdk`` package (see :func:`_read_now_playing_windows`). ``winsdk`` is
  Windows-only and OPTIONAL (see tools/lazy_deps.py's
  "presence.media_watcher" entry / the `presence` extra in pyproject.toml).
* **Linux** -- MPRIS (Media Player Remote Interfacing Specification) over
  the D-Bus session bus, queried via the ``busctl`` CLI (part of systemd;
  see :func:`_read_now_playing_linux`). No extra pip dependency -- see the
  module docstring section on that choice below.
* **macOS** -- AppleScript queries against Spotify and Music.app via
  ``osascript`` (see :func:`_read_now_playing_macos`). Deliberately does
  NOT use the private MediaRemote framework; browser-played media is not
  observable this way (see the ``# ponytail:`` comment on that function).

On any platform, or when the platform's tooling isn't available, this
module degrades to a clear one-line message instead of crashing -- it
simply has no now-playing data to contribute.

Run directly:
    python -m tools.presence.media_watcher

Managed by `hermes presence setup` / `pause` / `resume` (see
hermes_cli/presence_cmd.py), which spawns this as a detached background
process and tracks its PID under ~/.hermes/presence/media_watcher.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

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

LINUX_MPRIS_INSTALL_HINT = (
    "Now-playing tracking needs 'busctl' (ships with systemd) to query "
    "MPRIS-compatible media players over the D-Bus session bus. Most "
    "desktop Linux distros already have it; if not, install your distro's "
    "systemd package. Presence keeps working without it -- you just won't "
    "get now-playing data."
)

MACOS_OSASCRIPT_INSTALL_HINT = (
    "Now-playing tracking needs 'osascript' (ships with every macOS "
    "install) to query Spotify/Music.app via AppleScript. If it's "
    "missing something is unusual about this machine's setup. Presence "
    "keeps working without it -- you just won't get now-playing data, "
    "and browser-played media is never captured this way regardless "
    "(see the ponytail note on _read_now_playing_macos)."
)

_PLAYBACK_STATUS_NAMES = {
    0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused",
}

DBUS_SUBPROCESS_TIMEOUT_SECONDS = 3.0
OSASCRIPT_SUBPROCESS_TIMEOUT_SECONDS = 3.0

MPRIS_BUS_PREFIX = "org.mpris.MediaPlayer2."

# Apps queried for macOS now-playing, in poll order. Both ship an official
# AppleScript scripting dictionary (player state / current track), so no
# private-API access is needed.
MACOS_MEDIA_APPS: tuple[str, ...] = ("Spotify", "Music")


def media_bucket_id(hostname: Optional[str] = None) -> str:
    return f"aw-watcher-media_{hostname or socket.gethostname()}"


# =============================================================================
# Windows -- SMTC via winsdk
# =============================================================================


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


def _read_now_playing_windows() -> Optional[Dict[str, Any]]:
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


# =============================================================================
# Linux -- MPRIS over D-Bus via busctl
#
# Transport choice: busctl (ships with systemd) over jeepney (pure-Python
# D-Bus lib), for three reasons --
#
#   1. No new PyPI dependency. Every other opt-in backend in this repo goes
#      through tools/lazy_deps.py's allowlist + pin-and-audit process; a
#      subprocess call to a binary that already ships with the target OS
#      needs none of that, and keeps the `presence` extra's blast radius
#      exactly where it is today (Windows-only winsdk).
#   2. Testability without a live bus. busctl's `--json=short` output is a
#      clean text boundary -- the parsers below consume already-parsed JSON
#      dicts, so unit tests can feed canned payloads without a running
#      D-Bus session or event loop (jeepney would need a real socket, or
#      heavy mocking of its async I/O layer, to exercise realistically).
#   3. busctl is present wherever a full desktop session capable of running
#      MPRIS players already runs (GNOME/KDE/etc. all depend on systemd for
#      session management in practice). On distros without it, the adapter
#      degrades exactly like missing winsdk does: a clear log hint and no
#      now-playing data, never a crash.
# =============================================================================


def busctl_available() -> bool:
    return shutil.which("busctl") is not None


def _busctl_json(args: List[str]) -> Optional[Any]:
    """Run ``busctl --user --json=short <args>`` and parse stdout as JSON.

    Pure transport: returns the decoded JSON value on success, or None on
    ANY failure (missing binary, non-zero exit, timeout, malformed JSON).
    Never raises.
    """
    try:
        proc = subprocess.run(
            ["busctl", "--user", "--json=short", *args],
            capture_output=True, text=True, timeout=DBUS_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("busctl invocation failed: %s", exc)
        return None
    if proc.returncode != 0:
        logger.debug("busctl exited %s: %s", proc.returncode, proc.stderr)
        return None
    try:
        return json.loads(proc.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.debug("busctl JSON parse failed: %s", exc)
        return None


def _parse_mpris_bus_names(payload: Any) -> List[str]:
    """Pure parser: extract ``org.mpris.MediaPlayer2.*`` bus names from a
    decoded ``busctl call org.freedesktop.DBus ... ListNames`` payload.

    Accepts busctl's ``{"type": "as", "data": [...]}`` envelope, or a bare
    list (defensive -- keeps the parser directly testable with either
    shape). Returns [] for anything else, never raises.
    """
    if payload is None:
        return []
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    return [n for n in data if isinstance(n, str) and n.startswith(MPRIS_BUS_PREFIX)]


def _parse_mpris_playback_status(payload: Any) -> Optional[str]:
    """Pure parser for a decoded ``busctl get-property ... PlaybackStatus``
    payload. Returns a lowercased status string ("playing"/"paused"/
    "stopped"/...), or None when absent/malformed."""
    if payload is None:
        return None
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, str) or not data.strip():
        return None
    return data.strip().lower()


def _parse_mpris_metadata(payload: Any) -> Dict[str, str]:
    """Pure parser for a decoded ``busctl get-property ... Metadata``
    payload (MPRIS's ``a{sv}`` dict-of-variants). Returns
    ``{"title", "artist", "trackid"}`` with "" defaults for anything
    missing/malformed."""
    result = {"title": "", "artist": "", "trackid": ""}
    if payload is None:
        return result
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, dict):
        return result

    def _unwrap(key: str) -> Any:
        entry = data.get(key)
        if isinstance(entry, dict):
            return entry.get("data")
        return entry

    title = _unwrap("xesam:title")
    if isinstance(title, str):
        result["title"] = title

    artist = _unwrap("xesam:artist")
    if isinstance(artist, list):
        result["artist"] = ", ".join(a for a in artist if isinstance(a, str))
    elif isinstance(artist, str):
        result["artist"] = artist

    trackid = _unwrap("mpris:trackid")
    if isinstance(trackid, str):
        result["trackid"] = trackid

    return result


def _read_now_playing_linux() -> Optional[Dict[str, Any]]:
    """Query MPRIS players over the D-Bus session bus via ``busctl``.

    Enumerates ``org.mpris.MediaPlayer2.*`` bus names, prefers the first
    Playing player, falls back to the first Paused player when none are
    playing. Never raises -- any failure (missing busctl, no D-Bus
    session, no players, malformed output) returns None so the poll loop
    just skips that tick's heartbeat.
    """
    if not busctl_available():
        return None
    try:
        names_payload = _busctl_json([
            "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "ListNames",
        ])
        bus_names = _parse_mpris_bus_names(names_payload)
        if not bus_names:
            return None

        candidates: List[tuple[str, str]] = []
        for bus_name in bus_names:
            status_payload = _busctl_json([
                "get-property", bus_name, "/org/mpris/MediaPlayer2",
                "org.mpris.MediaPlayer2.Player", "PlaybackStatus",
            ])
            status = _parse_mpris_playback_status(status_payload)
            if status:
                candidates.append((bus_name, status))

        chosen = next((c for c in candidates if c[1] == "playing"), None)
        if chosen is None:
            chosen = next((c for c in candidates if c[1] == "paused"), None)
        if chosen is None:
            return None

        bus_name, status = chosen
        meta_payload = _busctl_json([
            "get-property", bus_name, "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player", "Metadata",
        ])
        meta = _parse_mpris_metadata(meta_payload)
        app_id = bus_name[len(MPRIS_BUS_PREFIX):] if bus_name.startswith(MPRIS_BUS_PREFIX) else bus_name
        return {
            "app_id": app_id,
            "title": meta["title"],
            "artist": meta["artist"],
            "status": status,
        }
    except Exception as exc:
        logger.debug("MPRIS query failed: %s", exc)
        return None


# =============================================================================
# macOS -- AppleScript (Spotify / Music.app) via osascript
# =============================================================================


def osascript_available() -> bool:
    return shutil.which("osascript") is not None


# Returns "not_running" when the target app isn't running, else
# "<state>|<title>|<artist>" -- e.g. "playing|Bohemian Rhapsody|Queen".
# Wrapped in try/on error inside the script itself so a track with missing
# name/artist properties (ads, some streams) never raises out of osascript.
_MACOS_APPLESCRIPT_TEMPLATE = """
if application "{app}" is running then
    tell application "{app}"
        set playerState to player state as string
        try
            set trackName to name of current track
        on error
            set trackName to ""
        end try
        try
            set trackArtist to artist of current track
        on error
            set trackArtist to ""
        end try
        return playerState & "|" & trackName & "|" & trackArtist
    end tell
else
    return "not_running"
end if
"""


def _query_macos_app(app_name: str) -> Optional[str]:
    """Run the AppleScript probe for one app via osascript (stdin).

    Pure transport: returns the raw stdout line on success, or None on ANY
    failure (missing osascript, timeout, non-zero exit). Never raises.
    """
    try:
        proc = subprocess.run(
            ["osascript"],
            input=_MACOS_APPLESCRIPT_TEMPLATE.format(app=app_name),
            capture_output=True, text=True, timeout=OSASCRIPT_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("osascript invocation failed for %s: %s", app_name, exc)
        return None
    if proc.returncode != 0:
        logger.debug("osascript exited %s for %s: %s", proc.returncode, app_name, proc.stderr)
        return None
    return proc.stdout.strip()


def _parse_macos_media_line(line: Optional[str], app_name: str) -> Optional[Dict[str, Any]]:
    """Pure parser for one line of :func:`_query_macos_app` output.

    Expected shapes: ``"not_running"``, or ``"<state>|<title>|<artist>"``.
    Returns None for not-running / empty / unparseable input, never raises.
    """
    if not line or line == "not_running":
        return None
    parts = line.split("|", 2)
    if len(parts) != 3:
        return None
    state, title, artist = parts
    state = state.strip().lower()
    if not state:
        return None
    return {
        "app_id": app_name,
        "title": title.strip(),
        "artist": artist.strip(),
        "status": state,
    }


def _read_now_playing_macos() -> Optional[Dict[str, Any]]:
    """Poll Spotify then Music.app via AppleScript; the first Playing app
    wins, falling back to the first Paused app when neither is playing.

    Never raises -- any failure (missing osascript, a script error, no app
    running) returns None so the poll loop just skips that tick's
    heartbeat.

    # ponytail: this only covers apps with an official AppleScript
    # scripting dictionary (Spotify, Music.app). Browser-played media
    # (YouTube / Spotify Web Player in Safari, Chrome, etc.) is NOT
    # observable through osascript -- there is no public browser scripting
    # interface for "now playing" metadata. Capturing that would require a
    # native helper built on the private MediaRemote framework (the same
    # one tools like `nowplaying-cli` / MediaRemoteAdapter wrap), which we
    # deliberately are not shipping here. Revisit if browser-media coverage
    # becomes a real ask.
    """
    if not osascript_available():
        return None
    try:
        candidates: List[Dict[str, Any]] = []
        for app_name in MACOS_MEDIA_APPS:
            line = _query_macos_app(app_name)
            media = _parse_macos_media_line(line, app_name)
            if media:
                candidates.append(media)

        chosen = next((m for m in candidates if m["status"] == "playing"), None)
        if chosen is None:
            chosen = next((m for m in candidates if m["status"] == "paused"), None)
        return chosen
    except Exception as exc:
        logger.debug("macOS now-playing query failed: %s", exc)
        return None


# =============================================================================
# Platform dispatch + poll loop
# =============================================================================


def get_current_media() -> Optional[Dict[str, Any]]:
    """Return ``{"app_id", "title", "artist", "status"}`` for whatever is
    currently playing, dispatched to the platform-appropriate adapter, or
    None when unavailable / nothing is playing.
    """
    if sys.platform == "win32":
        return _read_now_playing_windows()
    if sys.platform == "linux":
        return _read_now_playing_linux()
    if sys.platform == "darwin":
        return _read_now_playing_macos()
    return None


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
    """Poll the platform media session and post heartbeats to ActivityWatch
    until interrupted."""
    if sys.platform == "win32":
        if not ensure_winsdk(prompt=False):
            print(WINSDK_INSTALL_HINT)
            return 1
    elif sys.platform == "linux":
        if not busctl_available():
            print(LINUX_MPRIS_INSTALL_HINT)
            return 1
    elif sys.platform == "darwin":
        if not osascript_available():
            print(MACOS_OSASCRIPT_INSTALL_HINT)
            return 1
    else:
        print("presence media watcher: unsupported platform, exiting.")
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
