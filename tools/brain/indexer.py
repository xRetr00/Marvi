"""Incremental, dependency-light local folder indexer."""

from __future__ import annotations

import fnmatch
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from hermes_cli.config import cfg_get, load_config
from hermes_constants import get_hermes_home
from tools.brain.store import BrainStore
from tools.read_extract import extract_document_text, is_extractable_document

MAX_FILE_BYTES = 20 * 1024 * 1024
PLAIN_EXTENSIONS = frozenset(
    {".txt", ".md", ".rst", ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".yaml", ".yml", ".toml", ".csv", ".log", ".html", ".css", ".sql"}
)
DEFAULT_EXCLUDES = (".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__")
# Default cadence for the "Brain index" cron job (see ensure_index_job below).
# A full-text local index of a handful of folders doesn't need to run every
# 30 minutes; six hours keeps it fresh without needless disk churn/LLM-free
# background wakeups (spec 2026-07-14-marvi-deep-subconscious-brain-design.md §8).
DEFAULT_SCHEDULE = "every 6h"


def brain_config(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = config if config is not None else load_config()
    raw = cfg_get(cfg, "brain", default={}) or {}
    collect_raw = raw.get("collect") if isinstance(raw.get("collect"), dict) else {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "folders": [str(Path(item).expanduser()) for item in raw.get("folders", []) if str(item).strip()],
        "exclude": [str(item) for item in raw.get("exclude", DEFAULT_EXCLUDES)],
        "schedule": str(raw.get("schedule") or DEFAULT_SCHEDULE),
        # Self-feeding config (2026-07-20 auto-build pass): PC auto-discovery
        # (discovered folders live in the SEPARATE `auto_folders` list below —
        # `folders` above stays exactly what the user typed, never rewritten)
        # plus the email/GitHub document collectors. See tools/brain/discovery.py
        # and tools/brain/collectors/*.
        "auto_discover": bool(raw.get("auto_discover", True)),
        "max_auto_folders": int(raw.get("max_auto_folders", 5) or 5),
        "auto_folders": [str(item) for item in raw.get("auto_folders", []) if str(item).strip()],
        "collect_email": bool(collect_raw.get("email", True)),
        "collect_github": bool(collect_raw.get("github", True)),
        "github_max_repos": int(collect_raw.get("github_max_repos", 10) or 10),
    }


def _last_run_path() -> Path:
    return get_hermes_home() / "brain" / "last_run.json"


def read_last_run() -> Dict[str, Any]:
    """Return the persisted stats from the most recent index pass, if any."""
    path = _last_run_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"at": None, "indexed": 0, "skipped": 0, "removed": 0, "errors": 0}
    if not isinstance(data, dict):
        return {"at": None, "indexed": 0, "skipped": 0, "removed": 0, "errors": 0}
    return data


def _write_last_run(result: Dict[str, Any]) -> None:
    path = _last_run_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        "indexed": result.get("indexed", 0),
        "skipped": result.get("skipped", 0),
        "removed": result.get("removed", 0),
        "errors": result.get("errors", 0),
    }
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".last_run_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _last_collect_path() -> Path:
    return get_hermes_home() / "brain" / "collect_last_run.json"


def read_last_collect() -> Dict[str, Any]:
    """Return the persisted stats from the most recent collector pass (email +
    GitHub), if any. Mirrors :func:`read_last_run`'s shape/robustness."""
    path = _last_collect_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"at": None, "email": None, "github": None}
    if not isinstance(data, dict):
        return {"at": None, "email": None, "github": None}
    return data


def _write_last_collect(result: Dict[str, Any]) -> None:
    path = _last_collect_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": datetime.now(timezone.utc).isoformat(), **result}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".collect_last_run_", suffix=".tmp")
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


def brain_status(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Aggregate config + index store stats + last-run/discovery/collect info
    for the Brain tab/status endpoint (additive: existing consumers reading
    only the original keys are unaffected)."""
    from tools.brain.collected import collected_counts
    from tools.brain.discovery import read_last_discovery

    cfg = brain_config(config)
    store = BrainStore()
    try:
        stats = store.status()
    finally:
        store.close()
    return {
        **cfg,
        **stats,
        "last_run": read_last_run(),
        # Self-feeding status (2026-07-20 auto-build pass).
        "discovered_folders": cfg["auto_folders"],
        "last_discovery": read_last_discovery(),
        "collected": collected_counts(),
        "last_collect": read_last_collect(),
    }


def _excluded(path: Path, patterns: Iterable[str]) -> bool:
    text = str(path)
    return any(part in patterns or any(fnmatch.fnmatch(text, pattern) for pattern in patterns) for part in path.parts)


def _extract(path: Path) -> str:
    if path.suffix.lower() in PLAIN_EXTENSIONS:
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            return ""
        return data.decode("utf-8", errors="replace")
    if is_extractable_document(str(path)):
        return extract_document_text(str(path))
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader

            return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
        except Exception:
            return ""
    return ""


def _chunks(text: str, size: int = 1200, overlap: int = 160) -> List[str]:
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return []
    return [normalized[start : start + size] for start in range(0, len(normalized), size - overlap)]


def index_single_document(path: str | Path) -> Dict[str, Any]:
    """Index one specific file immediately, outside the folder-scan loop.

    Used by ``brain_store_document`` (tools/brain_ingest_tool.py, via
    tools/brain/collected.py) and the email/GitHub collectors so a document
    written to ``HERMES_HOME/brain/collected/...`` becomes searchable right
    away instead of waiting for the next scheduled "Brain indexer" pass.
    """
    target = Path(path).expanduser().resolve()
    store = BrainStore()
    try:
        if not target.is_file():
            return {"ok": False, "error": f"Not a file: {target}"}
        try:
            stat = target.stat()
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        chunks = _chunks(_extract(target))
        if not chunks:
            return {"ok": False, "error": "No extractable text", "path": str(target)}
        store.replace_file(
            str(target), stat.st_mtime, stat.st_size, datetime.now(timezone.utc).isoformat(), chunks
        )
        return {"ok": True, "path": str(target), "chunks": len(chunks)}
    finally:
        store.close()


def index_configured_folders(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = brain_config(config)
    store = BrainStore()
    live: set[str] = set()
    indexed = skipped = errors = 0
    # Union manually-configured folders with auto-discovered ones (see
    # tools/brain/discovery.py) -- `cfg["folders"]` is never rewritten by
    # discovery, so this is the one place the two lists actually merge for
    # scanning purposes. dict.fromkeys dedups while preserving order.
    folders = list(dict.fromkeys([*cfg["folders"], *cfg["auto_folders"]]))
    try:
        for root_text in folders:
            root = Path(root_text).expanduser().resolve()
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or _excluded(path, cfg["exclude"]):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size > MAX_FILE_BYTES:
                    skipped += 1
                    continue
                canonical = str(path.resolve())
                live.add(canonical)
                prior = store.indexed_file(canonical)
                if prior and prior["mtime"] == stat.st_mtime and prior["size"] == stat.st_size:
                    skipped += 1
                    continue
                try:
                    chunks = _chunks(_extract(path))
                    if not chunks:
                        skipped += 1
                        continue
                    store.replace_file(
                        canonical, stat.st_mtime, stat.st_size, datetime.now(timezone.utc).isoformat(), chunks
                    )
                    indexed += 1
                except Exception:
                    errors += 1
        removed = store.remove_missing(live)
        result = {"ok": True, "indexed": indexed, "skipped": skipped, "removed": removed, "errors": errors, **store.status()}
        _write_last_run(result)
        return result
    finally:
        store.close()


def run_brain_indexer_job() -> Dict[str, Any]:
    """Entry point for the "Brain indexer" cron job -- the self-feeding
    pipeline: a throttled (once/24h) PC auto-discovery pass, then the
    email/GitHub document collectors (each a clean no-op skip when
    unconfigured), then the ordinary folder scan/index.

    This is the ONLY caller that runs discovery + collectors automatically --
    ``index_configured_folders()`` itself (used directly by ``hermes brain
    index``, ``POST /api/brain/index``, and every existing test) is
    unchanged, so manual reindexes stay fast and deterministic. Once a
    scheduled run has populated ``brain.auto_folders`` and collected
    documents, though, every subsequent ``index_configured_folders()`` call
    (manual or scheduled) picks them up automatically -- discovery just
    decides what's IN scope, indexing is what makes it searchable.
    """
    from hermes_cli.config import load_config, save_config
    from tools.brain.discovery import run_discovery

    cfg = load_config()
    discovery_result = run_discovery(cfg)
    if discovery_result.get("ran"):
        save_config(cfg)

    brain = dict(cfg.get("brain") or {})
    collect_cfg = brain.get("collect") if isinstance(brain.get("collect"), dict) else {}
    collect_result: Dict[str, Any] = {"email": None, "github": None}

    if bool(collect_cfg.get("email", True)):
        try:
            from tools.brain.collectors.email_docs import collect_email_documents

            collect_result["email"] = collect_email_documents()
        except Exception as exc:  # a collector must never take the indexer down
            collect_result["email"] = {"ok": False, "error": str(exc)}

    if bool(collect_cfg.get("github", True)):
        try:
            from tools.brain.collectors.github_docs import collect_github_documents

            collect_result["github"] = collect_github_documents(
                max_repos=int(collect_cfg.get("github_max_repos", 10) or 10)
            )
        except Exception as exc:
            collect_result["github"] = {"ok": False, "error": str(exc)}

    _write_last_collect(collect_result)

    index_result = index_configured_folders(cfg)
    return {**index_result, "discovery": discovery_result, "collect": collect_result}


def ensure_index_job(config: Dict[str, Any]) -> Dict[str, Any]:
    """Create/resume the standard no-agent cron index job idempotently."""
    from cron.jobs import create_job, get_job, resume_job, update_job

    brain = dict(config.get("brain") or {})
    scripts = get_hermes_home() / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shim = scripts / "brain_index.py"
    shim.write_text(
        "from tools.brain.indexer import run_brain_indexer_job\n"
        "import json\n"
        "print(json.dumps(run_brain_indexer_job()))\n",
        encoding="utf-8",
    )
    schedule = str(brain.get("schedule") or DEFAULT_SCHEDULE)
    job = get_job(brain.get("job_id")) if brain.get("job_id") else None
    if job is None:
        job = create_job(
            prompt=None,
            schedule=schedule,
            name="Brain index",
            script=shim.name,
            no_agent=True,
            deliver="local",
        )
        brain["job_id"] = job["id"]
    else:
        if job.get("state") == "paused":
            job = resume_job(job["id"]) or job
        if job.get("schedule_display") != schedule:
            update_job(job["id"], {"schedule": schedule})
    config["brain"] = brain
    return job
