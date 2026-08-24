"""Журнал збоїв. Один рядок JSON на подію, щоб потім було видно, що впало."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class ErrorLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.count = 0

    def record(self, stage: str, message: str, exc: BaseException | None = None) -> None:
        self.count += 1
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "message": message,
        }
        if exc is not None:
            entry["exception"] = f"{type(exc).__name__}: {exc}"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
