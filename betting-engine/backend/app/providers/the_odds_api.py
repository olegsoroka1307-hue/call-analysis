"""Адаптер The Odds API v4 (ТЗ §6).

Парсер payload'а спільний для реального HTTP-транспорту і для офлайн-реплею,
тому офлайн-прогін виконує рівно той самий код нормалізації, що й прод.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import get_settings
from .base import OddsProvider, ProviderError, ProviderEvent, ProviderOdds, Transport

#: Ринки The Odds API -> внутрішні коди.
MARKET_MAP = {
    "totals": "TOTALS",
    "team_totals": "TEAM_TOTALS",
    "btts": "BTTS",
    "h2h": "H2H",
    "spreads": "SPREADS",
}

SELECTION_MAP = {"over": "OVER", "under": "UNDER", "yes": "YES", "no": "NO"}

#: Для TEAM_TOTALS selection доповнюється префіксом HOME_/AWAY_ під час парсингу.


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class HttpTransport:
    """Реальний HTTP-транспорт до api.the-odds-api.com."""

    data_source = "LIVE"

    def __init__(self, api_key: str, base_url: str, timeout: float = 20.0) -> None:
        if not api_key:
            raise ProviderError(
                "ODDS_API_KEY не заданий — реальні дані недоступні. "
                "ТЗ §1: не вигадувати дані, повертати DATA UNAVAILABLE."
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self._base_url}/{path.lstrip('/')}"
        try:
            response = self._client.get(url, params={**params, "apiKey": self._api_key})
        except httpx.HTTPError as exc:
            raise ProviderError(f"transport failure for {url}: {exc}") from exc
        if response.status_code != 200:
            raise ProviderError(
                f"{url} -> HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json()


class TheOddsApiProvider(OddsProvider):
    def __init__(self, transport: Transport, regions: str = "eu") -> None:
        self._transport = transport
        self._regions = regions

    @property
    def data_source(self) -> str:
        return self._transport.data_source

    # -- OddsProvider ------------------------------------------------------

    def get_events(self, sport_key: str) -> list[ProviderEvent]:
        payload = self._transport.get(f"sports/{sport_key}/odds", self._odds_params(["totals"]))
        return self.parse_events(payload)

    def get_markets(self, sport_key: str) -> list[str]:
        return sorted(MARKET_MAP.values())

    def get_odds(self, sport_key: str, markets: list[str]) -> list[ProviderOdds]:
        payload = self._transport.get(f"sports/{sport_key}/odds", self._odds_params(markets))
        return self.parse_odds(payload)

    def get_historical_odds(
        self, sport_key: str, markets: list[str], at: datetime
    ) -> list[ProviderOdds]:
        params = {**self._odds_params(markets), "date": at.astimezone(timezone.utc).isoformat()}
        payload = self._transport.get(f"historical/sports/{sport_key}/odds", params)
        # Historical endpoint загортає результат у {"timestamp":..., "data":[...]}.
        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
        return self.parse_odds(payload)

    def get_bookmakers(self, sport_key: str) -> list[tuple[str, str]]:
        payload = self._transport.get(f"sports/{sport_key}/odds", self._odds_params(["totals"]))
        seen: dict[str, str] = {}
        for event in payload:
            for bookmaker in event.get("bookmakers", []):
                seen.setdefault(bookmaker["key"], bookmaker.get("title", bookmaker["key"]))
        return sorted(seen.items())

    # -- Парсинг (спільний для обох транспортів) ---------------------------

    def _odds_params(self, markets: list[str]) -> dict[str, Any]:
        return {
            "regions": self._regions,
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }

    @staticmethod
    def parse_events(payload: Any) -> list[ProviderEvent]:
        return [
            ProviderEvent(
                provider_event_id=event["id"],
                sport_key=event["sport_key"],
                league_name=event.get("sport_title", event["sport_key"]),
                commence_time=_parse_ts(event["commence_time"]),
                home_team=event["home_team"],
                away_team=event["away_team"],
            )
            for event in payload
        ]

    @staticmethod
    def parse_odds(payload: Any) -> list[ProviderOdds]:
        out: list[ProviderOdds] = []
        for event in payload:
            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    market_code = MARKET_MAP.get(market["key"])
                    if market_code is None:
                        continue  # незнайомий ринок ігнорується, а не вгадується
                    timestamp = _parse_ts(
                        market.get("last_update") or bookmaker["last_update"]
                    )
                    for outcome in market.get("outcomes", []):
                        selection = SELECTION_MAP.get(outcome["name"].strip().lower())
                        if selection is None:
                            continue
                        if market_code == "TEAM_TOTALS":
                            # The Odds API називає команду в полі "description".
                            team = (outcome.get("description") or "").strip()
                            if team == event["home_team"]:
                                selection = f"HOME_{selection}"
                            elif team == event["away_team"]:
                                selection = f"AWAY_{selection}"
                            else:
                                continue  # невідома команда -> пропускаємо, не вгадуємо
                        out.append(
                            ProviderOdds(
                                provider_event_id=event["id"],
                                bookmaker_key=bookmaker["key"],
                                bookmaker_title=bookmaker.get("title", bookmaker["key"]),
                                market_code=market_code,
                                selection=selection,
                                line=outcome.get("point"),
                                odds=float(outcome["price"]),
                                source_timestamp=timestamp,
                            )
                        )
        return out


def build_http_provider() -> TheOddsApiProvider:
    settings = get_settings()
    transport = HttpTransport(settings.odds_api_key, settings.odds_api_base_url)
    return TheOddsApiProvider(transport, regions=settings.odds_api_regions)
