"""Памʼять про вже показані замовлення."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class State:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS seen ("
            " key TEXT PRIMARY KEY, seen_at TEXT NOT NULL,"
            " score INTEGER NOT NULL, title TEXT NOT NULL)"
        )
        self.conn.commit()

    @staticmethod
    def key(source: str, project_id: str) -> str:
        return f"{source}:{project_id}"

    def seen(self, source: str, project_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM seen WHERE key = ?", (self.key(source, project_id),)
        )
        return cur.fetchone() is not None

    def mark(self, source: str, project_id: str, score: int, title: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO seen (key, seen_at, score, title) VALUES (?,?,?,?)",
            (
                self.key(source, project_id),
                datetime.now(timezone.utc).isoformat(),
                score,
                title[:200],
            ),
        )
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def close(self) -> None:
        self.conn.close()
