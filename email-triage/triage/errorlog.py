"""Журнал помилок: JSONL на диску + лічильники для підсумку прогону."""
from __future__ import annotations

import json
import logging
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("triage")


class ErrorLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.counts: Counter[str] = Counter()
        self.entries: list[dict] = []

    def record(
        self,
        stage: str,
        message: str,
        *,
        message_id: str | None = None,
        exc: BaseException | None = None,
        fatal: bool = False,
    ) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "message_id": message_id,
            "error": message,
            "fatal": fatal,
        }
        if exc is not None:
            entry["exception"] = type(exc).__name__
            entry["traceback"] = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )[-4000:]
        self.counts[stage] += 1
        self.entries.append(entry)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log.warning("[%s] %s%s", stage, message, f" ({message_id})" if message_id else "")

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        if not self.counts:
            return "Помилок немає."
        parts = ", ".join(f"{stage}: {n}" for stage, n in sorted(self.counts.items()))
        return f"Помилок {self.total} ({parts}). Деталі: {self.path}"
