"""Памʼять про вже оброблені листи, щоб повторний запуск не дублював рядки."""
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
            "CREATE TABLE IF NOT EXISTS processed ("
            " message_id TEXT PRIMARY KEY,"
            " processed_at TEXT NOT NULL,"
            " label TEXT NOT NULL)"
        )
        self.conn.commit()

    def seen(self, message_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM processed WHERE message_id = ?", (message_id,)
        )
        return cur.fetchone() is not None

    def mark(self, message_id: str, label: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO processed (message_id, processed_at, label)"
            " VALUES (?, ?, ?)",
            (message_id, datetime.now(timezone.utc).isoformat(), label),
        )
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]

    def close(self) -> None:
        self.conn.close()
