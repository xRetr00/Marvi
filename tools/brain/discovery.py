"""PC auto-discovery for the Brain index.

Finds likely personal-document folders on the user's own machine, ranks
them by "document density" (how many indexable files sit directly inside),
and auto-adds the top few into a SEPARATE config list, ``brain.auto_folders``
-- never touching ``brain.folders``, the list the user typed themselves.
``tools/brain/indexer.py``'s ``index_configured_folders`` unions the two
lists when it actually scans, so once a folder is discovered it's indexed
exactly like a manually-added one, but the user's own list stays exactly
what they wrote (the Brain tab can therefore show "yours" vs "discovered"
as two distinct, independently removable lists).

Two layers:

* :func:`discover_document_folders` -- pure, side-effect-free ranking. Given
  a home directory, an exclude list, and the set of folders already in use,
  returns the top-N candidate folders by document count. Fully unit
  testable with a fake ``home`` directory.
* :func:`run_discovery` -- the throttled (once/24h, via an on-disk state
  stamp) orchestration used by the "Brain indexer" cron job
  (``tools/brain/indexer.py::run_brain_indexer_job``). Mutates
  ``cfg["brain"]["auto_folders"]`` in place and returns a summary; mirrors
  ``ensure_index_job``'s contract of mutating-but-not-saving the passed
  config, leaving ``save_config`` to the caller.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from hermes_constants import get_hermes_home
from tools.brain.indexer import PLAIN_EXTENSIONS, _excluded, brain_config
from tools.read_extract import EXTRACTABLE_EXTENSIONS

# Defaults mirrored by tools/brain/indexer.py::brain_config() -- kept here
# too so discovery.py's own defaults are self-documenting for anyone reading
# just this module.
DEFAULT_MAX_AUTO_FOLDERS = 5
DEFAULT_AUTO_DISCOVER = True

# Once/24h throttle for the discovery pass (independent of the "Brain
# indexer" cron cadence itself, which defaults to every 6h -- discovery is
# far cheaper to skip than to run, so it gets its own, coarser floor).
DISCOVERY_INTERVAL_SECONDS = 24 * 60 * 60

# Extensions counted toward a folder's "document density" score: the
# plain-text set the indexer chunks directly, the extractable-document set
# (.docx/.xlsx/.ipynb), and .pdf (indexer._extract handles these too).
DOC_EXTENSIONS = frozenset(PLAIN_EXTENSIONS | EXTRACTABLE_EXTENSIONS | {".pdf"})

# Root folders to probe under the user's profile. Windows paths resolve via
# Path.home() (works the same on POSIX for parity/testability).
CANDIDATE_ROOT_NAMES = ("Documents", "Desktop", "Downloads")

# Presence of a VCS marker is the generic "this is a code repo, not a
# personal document stash" signal -- excluded even if the folder name isn't
# covered by the indexer's own DEFAULT_EXCLUDES patterns.
CODE_MARKER = ".git"

# Cap file-count scanning per candidate folder so a huge synced folder (e.g.
# a OneDrive "Documents" with tens of thousands of files) can't turn one
# discovery pass into a slow full walk. Density is a *ranking signal*, not
# an exact count, and the scan itself is deliberately non-recursive (see
# _count_indexable_files) so this cap is really just a belt-and-braces
# safety net for pathologically large single-level folders.
MAX_FILES_SCANNED_PER_FOLDER = 500


def _is_code_heavy(folder: Path) -> bool:
    try:
        return (folder / CODE_MARKER).exists()
    except OSError:
        return False


def _count_indexable_files(folder: Path, exclude: Iterable[str]) -> int:
    """Shallow (top-level-only) count of indexable files directly inside
    *folder*, capped at :data:`MAX_FILES_SCANNED_PER_FOLDER`.

    Deliberately non-recursive: discovery ranks a folder by its OWN document
    density, not its entire subtree, and a plain ``scandir()`` keeps a
    folder with a huge nested subtree from turning discovery into a slow
    full walk (that cost belongs to the indexer's own incremental rglob,
    which only ever touches folders that already made the cut).
    """
    count = 0
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if count >= MAX_FILES_SCANNED_PER_FOLDER:
                    break
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                if Path(entry.name).suffix.lower() not in DOC_EXTENSIONS:
                    continue
                if _excluded(Path(entry.path), exclude):
                    continue
                count += 1
    except OSError:
        return 0
    return count


def _candidate_folders(home: Path) -> List[Path]:
    """Documents/Desktop/Downloads themselves, plus one level of their
    subfolders -- the scope the design calls for."""
    roots = [home / name for name in CANDIDATE_ROOT_NAMES if (home / name).is_dir()]
    candidates: List[Path] = list(roots)
    for root in roots:
        try:
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            candidates.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return candidates


def discover_document_folders(
    *,
    home: Path | None = None,
    exclude: Iterable[str] = (),
    already_used: Iterable[str] = (),
    max_folders: int = DEFAULT_MAX_AUTO_FOLDERS,
) -> List[Dict[str, Any]]:
    """Scan the user's likely document roots and rank candidates by
    indexable-document density.

    Returns up to *max_folders* ``{"path": str, "count": int}`` dicts sorted
    by descending file count. Skips: folders already in *already_used*
    (resolved-path compared, so a manually-configured folder is never
    re-suggested), code-heavy folders (a ``.git`` directly inside), and
    anything matching *exclude* (the same exclude patterns the indexer
    itself honors).
    """
    home = home or Path.home()
    used_norm = {str(Path(p).expanduser().resolve()) for p in already_used if str(p).strip()}
    exclude = list(exclude)

    scored: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for folder in _candidate_folders(home):
        try:
            resolved = folder.resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen or key in used_norm:
            continue
        seen.add(key)
        if not resolved.is_dir():
            continue
        if _excluded(resolved, exclude):
            continue
        if _is_code_heavy(resolved):
            continue
        count = _count_indexable_files(resolved, exclude)
        if count <= 0:
            continue
        scored.append({"path": key, "count": count})

    scored.sort(key=lambda item: item["count"], reverse=True)
    return scored[: max(0, int(max_folders))]


# ---------------------------------------------------------------------------
# Throttled orchestration (state stamp + config mutation)
# ---------------------------------------------------------------------------


def _last_discovery_path() -> Path:
    return get_hermes_home() / "brain" / "discovery_last_run.json"


def read_last_discovery() -> Dict[str, Any]:
    """Return the persisted result of the most recent discovery pass, if
    any. Never raises -- a missing/corrupt state file just means "never run
    yet", exactly like ``tools/brain/indexer.py::read_last_run``."""
    path = _last_discovery_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"at": None, "folders": []}
    if not isinstance(data, dict):
        return {"at": None, "folders": []}
    return data


def _write_last_discovery(record: Dict[str, Any]) -> None:
    path = _last_discovery_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".discovery_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _due_for_discovery(last: Dict[str, Any]) -> bool:
    at = last.get("at")
    if not at:
        return True
    try:
        last_dt = datetime.fromisoformat(str(at))
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_dt).total_seconds() >= DISCOVERY_INTERVAL_SECONDS


def run_discovery(cfg: Dict[str, Any], *, force: bool = False) -> Dict[str, Any]:
    """Run (or skip, if throttled/disabled) a discovery pass and update
    ``cfg["brain"]["auto_folders"]`` in place.

    Mirrors ``ensure_index_job``'s contract: mutates the passed-in config
    dict but does NOT persist it -- the caller (``run_brain_indexer_job``)
    decides whether/when to ``save_config``. Only ever READS
    ``cfg["brain"]["folders"]`` (to exclude those paths from the
    discovered/auto set) -- the manually-configured list itself is never
    written to.

    Returns ``{"ran": bool, "reason"?: str, "at": ..., "folders": [...]}``.
    """
    brain = dict(cfg.get("brain") or {})
    auto_discover = bool(brain.get("auto_discover", DEFAULT_AUTO_DISCOVER))
    last = read_last_discovery()
    if not auto_discover:
        return {"ran": False, "reason": "disabled", **last}
    if not force and not _due_for_discovery(last):
        return {"ran": False, "reason": "throttled", **last}

    resolved = brain_config(cfg)
    manual = resolved["folders"]
    exclude = resolved["exclude"]
    max_folders = int(brain.get("max_auto_folders", DEFAULT_MAX_AUTO_FOLDERS) or DEFAULT_MAX_AUTO_FOLDERS)

    discovered = discover_document_folders(exclude=exclude, already_used=manual, max_folders=max_folders)
    auto_folders = [item["path"] for item in discovered]

    brain["auto_folders"] = auto_folders
    cfg["brain"] = brain

    record = {"at": datetime.now(timezone.utc).isoformat(), "folders": discovered}
    _write_last_discovery(record)
    return {"ran": True, **record}
