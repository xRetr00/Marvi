"""Shared write/dedup chokepoint for everything the Brain "collects" itself.

Every self-feeding path -- ``brain_store_document`` (chat/subconscious/
reflection/dreaming, via tools/brain_ingest_tool.py) and the email/GitHub
collectors (tools/brain/collectors/*) -- funnels through
:func:`write_collected_document` so dedup, on-disk layout, and immediate
indexing behave identically no matter which surface produced the document.

Layout: ``HERMES_HOME/brain/collected/<source-slug>/<safe-title>.md``, one
``.manifest.json`` per source directory mapping a dedup key (an explicit
``ref`` when given, else a content hash) to ``{"path", "hash", "title"}`` so
a repeated save of the same (source, ref) with unchanged content is a fast
no-op -- no duplicate file, no wasted re-chunk/re-embed.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, *, max_len: int = 80) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", normalized.lower()).strip("-")
    return (slug or "untitled")[:max_len]


def collected_root() -> Path:
    return get_hermes_home() / "brain" / "collected"


def collected_dir(source: str) -> Path:
    d = collected_root() / _slugify(source)
    d.mkdir(parents=True, exist_ok=True)
    return d


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def dedup_key(ref: Optional[str], text: str) -> str:
    """Stable manifest key: an explicit ref when given, else a content hash."""
    return f"ref:{ref}" if ref else f"hash:{content_hash(text)}"


def _manifest_path(source: str) -> Path:
    return collected_dir(source) / ".manifest.json"


def _load_manifest(source: str) -> Dict[str, Any]:
    path = _manifest_path(source)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_manifest(source: str, manifest: Dict[str, Any]) -> None:
    _manifest_path(source).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def _unique_path(directory: Path, slug: str, manifest: Dict[str, Any], key: str) -> Path:
    """Return a filename for *slug* that either belongs to *key* already or
    is free -- disambiguating a same-slugged-but-different-key collision
    (e.g. two differently-worded titles that slugify identically) with a
    numeric suffix instead of silently overwriting an unrelated document."""
    candidate = directory / f"{slug}.md"
    owner = next((k for k, v in manifest.items() if v.get("path") == str(candidate)), None)
    if owner is None or owner == key:
        return candidate
    n = 2
    while True:
        candidate = directory / f"{slug}-{n}.md"
        owner = next((k for k, v in manifest.items() if v.get("path") == str(candidate)), None)
        if owner is None or owner == key:
            return candidate
        n += 1


def write_collected_document(
    *,
    source: str,
    title: str,
    text: str,
    ref: Optional[str] = None,
    index: bool = True,
) -> Dict[str, Any]:
    """Write *text* under ``collected/<source-slug>/<safe-title>.md``.

    Skips the write (and re-index) when an entry with the same dedup key
    already has the same content hash on record, so an unchanged email
    attachment or repo README re-collected on the next pass is a fast
    no-op. Returns ``{"ok": True, "written": bool, "skipped": bool, "path": str}``,
    plus ``"index"`` (the ``index_single_document`` result) when *index* is
    True and the document was (re)written.
    """
    manifest = _load_manifest(source)
    key = dedup_key(ref, text)
    digest = content_hash(text)
    prior = manifest.get(key)
    if isinstance(prior, dict) and prior.get("hash") == digest and Path(str(prior.get("path", ""))).is_file():
        return {"ok": True, "written": False, "skipped": True, "path": prior["path"]}

    directory = collected_dir(source)
    slug = _slugify(title)
    path = _unique_path(directory, slug, manifest, key)

    if isinstance(prior, dict) and prior.get("path") and prior["path"] != str(path):
        # Title changed since the last save for this key -- clean up the old
        # file so collected/ doesn't accumulate orphans for the same logical
        # document under two different names.
        try:
            Path(prior["path"]).unlink(missing_ok=True)
        except OSError:
            pass

    body_lines = [f"# {title}", "", f"Source: {source}"]
    if ref:
        body_lines.append(f"Ref: {ref}")
    body_lines.extend(["", text.rstrip(), ""])
    path.write_text("\n".join(body_lines), encoding="utf-8")

    manifest[key] = {"path": str(path), "hash": digest, "title": title}
    _save_manifest(source, manifest)

    result: Dict[str, Any] = {"ok": True, "written": True, "skipped": False, "path": str(path)}
    if index:
        from tools.brain.indexer import index_single_document

        result["index"] = index_single_document(path)
    return result


def collected_counts() -> Dict[str, int]:
    """Return ``{source_slug: file_count}`` for everything under collected/
    -- used by the Brain status endpoint/tab to show per-source totals."""
    root = collected_root()
    if not root.is_dir():
        return {}
    counts: Dict[str, int] = {}
    for child in root.iterdir():
        if not child.is_dir():
            continue
        counts[child.name] = sum(1 for _ in child.glob("*.md"))
    return counts
