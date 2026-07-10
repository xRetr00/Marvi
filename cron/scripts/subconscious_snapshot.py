#!/usr/bin/env python3
"""Composio smart-sync snapshot script -- Contract 1 entry point.

Invoked by Marvi's subconscious-tick cron job (Workstream A,
``cron/subconscious.py``) as a pre-script: the tick runs this, and only pays
for an LLM pass when this script reports something actually changed.

Per Contract 1 (see the design spec)::

    The script prints to stdout either the literal line ``NO_CHANGE`` (tick
    exits, zero LLM cost) or a human-readable diff of what changed.

This script iterates the surfaces configured under ``composio.surfaces`` in
config.yaml, calls each surface's delta fetcher
(``cron/scripts/subconscious/<surface>.py::fetch_delta``), and aggregates.
Every fetch is a DELTA fetch against a locally-stored cursor
(``~/.hermes/subconscious/snapshots/<surface>.json``) -- never a blind full
refetch -- which is what keeps this cheap enough to run on every tick
without burning API rate limits (the explicit anti-goal from the design
spec: no OpenHuman-style polling waste).

A surface that isn't connected, is rate-limited, or errors out is logged to
stderr and skipped; it can never crash this script or block the other
surfaces from being checked.

Runnable standalone::

    python cron/scripts/subconscious_snapshot.py

Exits 0 always (a broken surface is a stderr warning + skip, not a process
failure) so the cron pre-script step never itself trips an error path in the
scheduler.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make the repo importable when this script is invoked directly
# (``python cron/scripts/subconscious_snapshot.py``) rather than through an
# already-on-sys.path editable/installed `hermes` entry point. Mirrors the
# project-root bootstrap used by the test suite for the same reason.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# The exact Contract 1 sentinel. A dedicated constant so callers that want
# to compare against it (tests, Workstream A's tick parser) import this
# instead of re-typing the literal.
NO_CHANGE_MARKER = "NO_CHANGE"


def _eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def _load_composio_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception as e:
        _eprint(f"subconscious_snapshot: could not load config ({e}); assuming no surfaces configured")
        return {}
    composio_cfg = (config or {}).get("composio") if isinstance(config, dict) else None
    return composio_cfg if isinstance(composio_cfg, dict) else {}


def _configured_surfaces(composio_cfg: Dict[str, Any]) -> List[str]:
    surfaces = composio_cfg.get("surfaces")
    if not isinstance(surfaces, list):
        return []
    out: List[str] = []
    for s in surfaces:
        name = str(s or "").strip().lower()
        if name and name not in out:
            out.append(name)
    return out


def _min_interval_seconds(composio_cfg: Dict[str, Any]) -> int:
    from cron.scripts.subconscious.snapshot_store import DEFAULT_MIN_INTERVAL_SECONDS

    value = composio_cfg.get("min_interval_seconds")
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return DEFAULT_MIN_INTERVAL_SECONDS


def _quiet_backoff_max(composio_cfg: Dict[str, Any]) -> int:
    from cron.scripts.subconscious.snapshot_store import DEFAULT_QUIET_BACKOFF_MAX

    value = composio_cfg.get("quiet_backoff_max")
    if isinstance(value, (int, float)) and value >= 1:
        return int(value)
    return DEFAULT_QUIET_BACKOFF_MAX


def run() -> str:
    """Run one subconscious-tick sync pass over every configured surface.

    Returns the literal string ``"NO_CHANGE"`` when nothing changed
    anywhere (including "no surfaces configured"), otherwise a compact diff
    summary grouped by surface, one ``## <surface>`` section per surface
    that reported a change.
    """
    from cron.scripts.subconscious.base import get_fetcher, known_surfaces
    from cron.scripts.subconscious.snapshot_store import open_store

    composio_cfg = _load_composio_config()
    surfaces = _configured_surfaces(composio_cfg)
    if not surfaces:
        return NO_CHANGE_MARKER

    min_interval = _min_interval_seconds(composio_cfg)
    quiet_backoff_max = _quiet_backoff_max(composio_cfg)
    sections: List[str] = []

    for surface in surfaces:
        fetcher = get_fetcher(surface)
        if fetcher is None:
            _eprint(
                f"subconscious_snapshot: surface {surface!r} is configured but not "
                f"implemented yet (known surfaces: {', '.join(known_surfaces())}); skipping"
            )
            continue

        try:
            store = open_store(
                surface,
                min_interval_seconds=min_interval,
                quiet_backoff_max=quiet_backoff_max,
            )
        except Exception as e:
            _eprint(f"subconscious_snapshot: surface {surface!r} has an invalid snapshot store ({e}); skipping")
            continue

        skip_reason = store.skip_reason()
        if skip_reason:
            _eprint(f"subconscious_snapshot: surface {surface!r} skipped ({skip_reason})")
            continue

        store.mark_attempt()
        diff: Optional[str] = None
        try:
            diff = fetcher(store)
            store.record_success(changed=bool(diff))
        except Exception as e:
            # A failing surface must NEVER crash the tick or block the other
            # surfaces (design spec error-handling section: "Composio auth
            # failure -> surface marked broken in status, never crash the
            # tick"). record_failure() drives the exponential backoff that
            # keeps a broken surface from being hammered every tick.
            store.record_failure(str(e))
            _eprint(f"subconscious_snapshot: surface {surface!r} fetch failed: {e}")
        finally:
            store.save()

        if diff:
            sections.append(f"## {surface}\n{diff}")

    if not sections:
        return NO_CHANGE_MARKER

    return "\n\n".join(sections)


def main() -> int:
    try:
        output = run()
    except Exception as e:  # pragma: no cover - defensive last resort
        # Even a totally unexpected failure must not look like "something
        # changed" to the caller -- fail toward silence, not toward a
        # spurious LLM wake-up. Warn on stderr so it's diagnosable.
        _eprint(f"subconscious_snapshot: unexpected failure ({e}); reporting NO_CHANGE")
        output = NO_CHANGE_MARKER
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
