"""The tiny delta-fetcher interface every Composio surface module implements,
plus the surface-name -> fetcher registry the entry script iterates.

A fetcher module exposes one function::

    def fetch_delta(store: SurfaceStore) -> Optional[str]:
        ...

Contract:
  * Return ``None`` (or ``""``) when nothing changed, or on first run when
    the fetcher is only establishing its baseline cursor -- never dump the
    whole inbox/notification list just because it's the first tick.
  * Return a compact, human-readable diff summary string otherwise.
  * Let auth/rate-limit/SDK-missing errors propagate (as
    ``composio_client.ComposioAuthError`` / ``ComposioRateLimited`` /
    ``ComposioTransientError`` / ``ComposioUnavailable``, or really any
    exception) -- do NOT swallow them into a silent ``None``, which would
    look identical to "nothing changed" and hide a real outage. The entry
    script's per-surface try/except is what turns a raised error into a
    skipped-surface warning instead of a crashed tick.

Adding a new surface (calendar, slack, ...) is exactly one new module
implementing ``fetch_delta(store)`` plus one line in :data:`FETCHERS`.

Builtin (non-Composio) surfaces
--------------------------------
A handful of surfaces aren't Composio-backed at all -- there's no external
account to connect, so there's no ``composio.surfaces`` config entry to opt
them in. ``smart_room`` (the local room-state plugin) is the first of
these: it still implements ``fetch_delta(store)`` and lives in
:data:`FETCHERS` like any other surface, but ``subconscious_snapshot.py``
auto-includes it based on :data:`BUILTIN_SURFACES` -- a name -> cheap
"is the plugin actually active right now" probe, rather than reading it out
of composio config. The plugin package (and any pip extras it needs) may be
absent on installs that never enabled it, so the import is guarded: a
missing/broken import just omits the surface from both registries, exactly
like an unimplemented Composio surface omits itself from
:func:`known_surfaces`.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from cron.scripts.subconscious import calendar, github, gmail, slack
from cron.scripts.subconscious.snapshot_store import SurfaceStore

FetchDeltaFn = Callable[[SurfaceStore], Optional[str]]

# Surface name -> fetch_delta callable. This is the whole extension point:
# a calendar.py / slack.py fetcher module plus one entry here is all a new
# surface needs.
FETCHERS: Dict[str, FetchDeltaFn] = {
    gmail.APP: gmail.fetch_delta,
    github.APP: github.fetch_delta,
    calendar.APP: calendar.fetch_delta,
    slack.APP: slack.fetch_delta,
}

# Name -> zero-arg "does this builtin surface's plugin look active right
# now" probe. Cheap and local only (a stat() on the plugin's own state
# file) -- never a runtime RPC/network call. subconscious_snapshot.py
# consults this after the configured Composio surfaces to decide which
# builtin surfaces to auto-include in a given tick.
BuiltinActiveProbeFn = Callable[[], bool]
BUILTIN_SURFACES: Dict[str, BuiltinActiveProbeFn] = {}

try:
    from cron.scripts.subconscious import smart_room as _smart_room
except ImportError:  # plugin package (or a pip extra it needs) not installed
    _smart_room = None
else:
    FETCHERS[_smart_room.APP] = _smart_room.fetch_delta

    def _smart_room_looks_active() -> bool:
        """True when the smart_room plugin's runtime has saved state at
        least once. No RPC/network -- just a stat() on state.json."""
        try:
            from plugins.smart_room.runtime.state_store import state_path

            return state_path().is_file()
        except Exception:
            return False

    BUILTIN_SURFACES[_smart_room.APP] = _smart_room_looks_active


def known_surfaces() -> List[str]:
    """All surfaces this build knows how to fetch (independent of what the
    user has actually configured/connected)."""
    return sorted(FETCHERS.keys())


def composio_surfaces() -> List[str]:
    """Composio-backed fetchers available for proactive account sync."""
    return sorted(set(FETCHERS) - set(BUILTIN_SURFACES))


def get_fetcher(surface: str) -> Optional[FetchDeltaFn]:
    return FETCHERS.get(surface)
