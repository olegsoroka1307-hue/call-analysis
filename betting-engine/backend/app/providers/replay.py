"""Replay-транспорт: віддає записані payload'и замість мережевих запитів.

Навіщо: у цьому середовищі всі зовнішні odds-API заблоковані egress-політикою
(див. gate0/GATE0_REPORT.md), тож PoC має бути відтворюваним офлайн.

Принцип: підміняється ЛИШЕ мережа. Payload має точно ту саму форму, що й
відповідь The Odds API v4, і проходить через той самий парсер
(TheOddsApiProvider.parse_odds). Коли з'являться ключ і доступ — міняється
один рядок конфігурації, а не код.

Дані у data/ синтетичні. Позначка data_source="SYNTHETIC_REPLAY" протягнута
до самої API-відповіді, щоб їх неможливо було сплутати з реальним ринком (ТЗ §1).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import ProviderError, Transport
from .the_odds_api import TheOddsApiProvider


class ReplayTransport(Transport):
    data_source = "SYNTHETIC_REPLAY"

    def __init__(self, data_dir: str | Path) -> None:
        self._dir = Path(data_dir)
        manifest_path = self._dir / "replay_manifest.json"
        if not manifest_path.exists():
            raise ProviderError(
                f"replay dataset not found at {manifest_path}. "
                f"Згенеруйте його: python -m app.tools.make_replay_dataset"
            )
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self._polls: list[str] = self.manifest["polls"]
        self._cursor = 0

    @property
    def poll_count(self) -> int:
        return len(self._polls)

    @property
    def cursor(self) -> int:
        return self._cursor

    def seek(self, index: int) -> None:
        if not 0 <= index < len(self._polls):
            raise ProviderError(f"poll index {index} out of range (0..{len(self._polls) - 1})")
        self._cursor = index

    def advance(self) -> bool:
        """Перейти до наступного опитування. False — якщо це вже останнє."""
        if self._cursor + 1 >= len(self._polls):
            return False
        self._cursor += 1
        return True

    def get(self, path: str, params: dict[str, Any]) -> Any:
        payload = json.loads((self._dir / self._polls[self._cursor]).read_text(encoding="utf-8"))
        requested = {m.strip() for m in str(params.get("markets", "")).split(",") if m.strip()}
        if not requested:
            return payload
        # Фільтруємо ринки так само, як це робить реальний endpoint.
        filtered = []
        for event in payload:
            event = {**event, "bookmakers": [
                {**b, "markets": [m for m in b.get("markets", []) if m["key"] in requested]}
                for b in event.get("bookmakers", [])
            ]}
            filtered.append(event)
        return filtered


def build_replay_provider(data_dir: str | Path) -> tuple[TheOddsApiProvider, ReplayTransport]:
    transport = ReplayTransport(data_dir)
    return TheOddsApiProvider(transport), transport
