"""SQLite FTS5 storage for the local Brain index."""

from __future__ import annotations

import sqlite3
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

from hermes_constants import get_hermes_home


class BrainStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (get_hermes_home() / "brain" / "brain.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, mtime REAL NOT NULL, size INTEGER NOT NULL,
                indexed_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
                path UNINDEXED, chunk_index UNINDEXED, content,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )

    def close(self) -> None:
        self.conn.close()

    def indexed_file(self, path: str) -> Dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        return dict(row) if row else None

    def replace_file(self, path: str, mtime: float, size: int, indexed_at: str, chunks: Iterable[str]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
            self.conn.executemany(
                "INSERT INTO chunks(path, chunk_index, content) VALUES (?, ?, ?)",
                ((path, index, content) for index, content in enumerate(chunks)),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO files(path, mtime, size, indexed_at) VALUES (?, ?, ?, ?)",
                (path, mtime, size, indexed_at),
            )

    def remove_missing(self, live_paths: set[str]) -> int:
        existing = {row[0] for row in self.conn.execute("SELECT path FROM files")}
        stale = existing - live_paths
        with self.conn:
            for path in stale:
                self.conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
                self.conn.execute("DELETE FROM files WHERE path = ?", (path,))
        return len(stale)

    def search(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        terms = re.findall(r"[\w-]+", query, flags=re.UNICODE)
        if not terms:
            return []
        fts_query = " AND ".join(f'"{term}"' for term in terms[:12])
        rows = self.conn.execute(
            """SELECT path, chunk_index, snippet(chunks, 2, '[', ']', ' … ', 24) AS snippet,
                      bm25(chunks) AS score
               FROM chunks WHERE chunks MATCH ? ORDER BY score LIMIT ?""",
            (fts_query, max(1, min(int(limit), 20))),
        ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> Dict[str, Any]:
        files = self.conn.execute("SELECT count(*) FROM files").fetchone()[0]
        chunks = self.conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        indexed_at = self.conn.execute("SELECT max(indexed_at) FROM files").fetchone()[0]
        return {"files": files, "chunks": chunks, "indexed_at": indexed_at, "path": str(self.path)}
