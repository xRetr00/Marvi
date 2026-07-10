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


def known_surfaces() -> List[str]:
    """All surfaces this build knows how to fetch (independent of what the
    user has actually configured/connected)."""
    return sorted(FETCHERS.keys())


def get_fetcher(surface: str) -> Optional[FetchDeltaFn]:
    return FETCHERS.get(surface)
