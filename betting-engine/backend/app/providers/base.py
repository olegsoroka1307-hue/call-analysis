"""Provider abstraction (ТЗ §6).

Бізнес-логіка не залежить від конкретного API: сервіси працюють лише з
нормалізованими DTO нижче. Заміна провайдера = новий адаптер, без змін
у моделі, зберіганні чи API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderEvent:
    provider_event_id: str
    sport_key: str
    league_name: str
    commence_time: datetime
    home_team: str
    away_team: str


@dataclass(frozen=True)
class ProviderOdds:
    provider_event_id: str
    bookmaker_key: str
    bookmaker_title: str
    market_code: str          # TOTALS | TEAM_TOTALS | BTTS | H2H | SPREADS
    selection: str            # OVER | UNDER | YES | NO | HOME | DRAW | AWAY
    line: float | None
    odds: float
    source_timestamp: datetime


class Transport(Protocol):
    """Спосіб дістати сирий payload. Реальна мережа або записаний датасет."""

    #: Позначка походження даних, яка доходить аж до API-відповіді (ТЗ §1).
    data_source: str

    def get(self, path: str, params: dict[str, Any]) -> Any: ...


class OddsProvider(ABC):
    """Інтерфейс з ТЗ §6."""

    @abstractmethod
    def get_events(self, sport_key: str) -> list[ProviderEvent]: ...

    @abstractmethod
    def get_markets(self, sport_key: str) -> list[str]: ...

    @abstractmethod
    def get_odds(self, sport_key: str, markets: list[str]) -> list[ProviderOdds]: ...

    @abstractmethod
    def get_historical_odds(
        self, sport_key: str, markets: list[str], at: datetime
    ) -> list[ProviderOdds]: ...

    @abstractmethod
    def get_bookmakers(self, sport_key: str) -> list[tuple[str, str]]: ...


class ProviderError(RuntimeError):
    """Провайдер недоступний або відповів помилкою. Дані НЕ вигадуються (ТЗ §1)."""
