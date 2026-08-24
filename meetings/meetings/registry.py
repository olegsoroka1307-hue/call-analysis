"""Реєстр співробітників і памʼять про вже розібрані наради.

Одна база SQLite, дві таблиці. Реєстр потрібен, бо Telegram не дозволяє
написати людині за іменем чи @username — тільки за chat_id, і цей chat_id
зʼявляється лише після того, як людина сама напише боту `/start`.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Employee


def _normalize(name: str) -> str:
    """Ключ пошуку: без регістру, без пробілів по краях, без @ у нікнеймі."""
    return name.strip().lstrip("@").casefold()


def transcript_fingerprint(text: str) -> str:
    """Відбиток транскрипції — щоб та сама нарада не розсилалася двічі."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


class Registry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS employees ("
            " key TEXT PRIMARY KEY,"
            " name TEXT NOT NULL,"
            " chat_id INTEGER NOT NULL,"
            " username TEXT NOT NULL DEFAULT '',"
            " added_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meetings ("
            " fingerprint TEXT PRIMARY KEY,"
            " title TEXT NOT NULL,"
            " processed_at TEXT NOT NULL,"
            " commitments INTEGER NOT NULL DEFAULT 0)"
        )
        self.conn.commit()

    # ── співробітники ────────────────────────────────────────────────
    def add(self, name: str, chat_id: int, username: str = "") -> Employee:
        now = datetime.now(timezone.utc).isoformat()
        employee = Employee(name=name.strip(), chat_id=int(chat_id),
                            telegram_username=username.strip().lstrip("@"))
        rows = [(_normalize(employee.name), employee)]
        if employee.telegram_username:
            rows.append((_normalize(employee.telegram_username), employee))
        for key, emp in rows:
            self.conn.execute(
                "INSERT OR REPLACE INTO employees (key, name, chat_id, username, added_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, emp.name, emp.chat_id, emp.telegram_username, now),
            )
        self.conn.commit()
        return employee

    def find(self, name: str) -> Employee | None:
        """Шукає за іменем або за @username — люди на нараді звуться і так, і так."""
        key = _normalize(name)
        if not key:
            return None
        row = self.conn.execute(
            "SELECT name, chat_id, username FROM employees WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            # Друга спроба: на нараді кажуть «Саша», у реєстрі «Саша Петренко».
            row = self.conn.execute(
                "SELECT name, chat_id, username FROM employees"
                " WHERE key LIKE ? ORDER BY LENGTH(key) LIMIT 2",
                (key + " %",),
            ).fetchall()
            if len(row) != 1:
                # Двом Сашам однаково написати не можна — це не здогадка, це помилка.
                return None
            row = row[0]
        return Employee(name=row[0], chat_id=int(row[1]), telegram_username=row[2])

    def all(self) -> list[Employee]:
        rows = self.conn.execute(
            "SELECT DISTINCT name, chat_id, username FROM employees ORDER BY name"
        ).fetchall()
        return [Employee(name=r[0], chat_id=int(r[1]), telegram_username=r[2]) for r in rows]

    def remove(self, name: str) -> bool:
        employee = self.find(name)
        if employee is None:
            return False
        self.conn.execute("DELETE FROM employees WHERE chat_id = ?", (employee.chat_id,))
        self.conn.commit()
        return True

    # ── наради ───────────────────────────────────────────────────────
    def seen_meeting(self, fingerprint: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM meetings WHERE fingerprint = ?", (fingerprint,)
        )
        return cur.fetchone() is not None

    def mark_meeting(self, fingerprint: str, title: str, commitments: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meetings (fingerprint, title, processed_at, commitments)"
            " VALUES (?, ?, ?, ?)",
            (fingerprint, title, datetime.now(timezone.utc).isoformat(), commitments),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
