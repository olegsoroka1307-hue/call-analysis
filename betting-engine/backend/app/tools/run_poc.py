"""Повний прогін PoC (ТЗ §52 п.1-8) в одну команду.

    python -m app.tools.run_poc

Проходить усі опитування провайдера, зберігає fixtures та odds snapshots у
PostgreSQL, рахує movement, no-vig, Poisson, fair odds та EV і друкує звіт.
"""

from __future__ import annotations

import argparse

from sqlalchemy import func, select

from ..config import get_settings
from ..db.base import SessionLocal
from ..db.models import Bookmaker, Fixture, OddsSnapshot
from ..providers.base import ProviderError
from ..providers.replay import build_replay_provider
from ..providers.the_odds_api import build_http_provider
from ..services.ingest import IngestResult, ingest_poll
from ..services.movement import get_movement
from ..services.pricing import price_fixture

MARKETS = ["totals", "team_totals"]
SPORT_KEY = "soccer_epl"


def _run_ingest(session, settings) -> tuple[IngestResult, str]:
    if settings.odds_provider == "the_odds_api":
        provider = build_http_provider()
        result = ingest_poll(session, provider, SPORT_KEY, MARKETS, "LIVE")
        return result, "LIVE"

    provider, transport = build_replay_provider(settings.replay_data_dir)
    result = IngestResult()
    transport.seek(0)
    while True:
        ingest_poll(session, provider, SPORT_KEY, MARKETS, transport.data_source, result)
        if not transport.advance():
            break
    return result, transport.data_source


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=int, default=5, help="скільки матчів показати")
    args = parser.parse_args()
    settings = get_settings()

    with SessionLocal() as session:
        try:
            result, data_source = _run_ingest(session, settings)
        except ProviderError as exc:
            # ТЗ §1: не вигадувати дані — краще чесно впасти.
            raise SystemExit(f"PROVIDER UNAVAILABLE: {exc}") from exc

        print("=" * 78)
        print(f"ПРОГІН PoC — провайдер: {settings.odds_provider}  |  дані: {data_source}")
        if data_source == "SYNTHETIC_REPLAY":
            print("!! СИНТЕТИЧНІ ДАНІ — не реальний ринок (див. gate0/GATE0_REPORT.md)")
        print("=" * 78)
        print(f"[п.1-4] Інжест: {result}")

        fixtures = list(
            session.scalars(select(Fixture).order_by(Fixture.kickoff_at).limit(args.fixtures))
        )
        bookmakers = {b.id: b for b in session.scalars(select(Bookmaker))}
        preferred = next((b for b in bookmakers.values() if b.is_preferred), None)

        for fixture in fixtures:
            label = f"{fixture.home_team.name} vs {fixture.away_team.name}"
            count = session.scalar(
                select(func.count())
                .select_from(OddsSnapshot)
                .where(OddsSnapshot.fixture_id == fixture.id)
            )
            print("\n" + "-" * 78)
            print(f"{label}   kickoff {fixture.kickoff_at:%Y-%m-%d %H:%M UTC}   snapshots={count}")

            if preferred is not None:
                movement = get_movement(
                    session, fixture.id, "TOTALS", "OVER",
                    preferred.id, preferred.key, fixture.kickoff_at,
                )
                if movement:
                    print(
                        f"[п.5] {preferred.key} Over: "
                        f"opening {movement.opening_odds} @ {movement.opening_line} -> "
                        f"current {movement.current_odds} @ {movement.current_line} "
                        f"({movement.price_move * 100:+.1f}%, {movement.signal}); "
                        f"closing {movement.closing_odds}"
                    )

            _run, priced = price_fixture(
                session, fixture,
                lambda_home=settings.poc_lambda_home,
                lambda_away=settings.poc_lambda_away,
                model_version=settings.model_version,
            )
            interesting = [
                p for p in priced
                if preferred is None or p.bookmaker == preferred.key
            ]
            header = (
                f"{'ринок':<12}{'сел.':<12}{'лін.':>6}{'кеф':>7}"
                f"{'ринок%':>9}{'модель%':>10}{'fair':>8}{'EV':>9}  рішення"
            )
            print(f"[п.6-8] {header}")
            for item in interesting:
                market_pct = (
                    f"{item.market_probability * 100:8.2f}"
                    if item.market_probability is not None else "     n/a"
                )
                fair = f"{item.fair_odds:7.3f}" if item.fair_odds else "    n/a"
                print(
                    f"        {item.market_code:<12}{item.selection:<12}{item.line:>6}"
                    f"{item.bookmaker_odds:>7.2f}{market_pct}"
                    f"{item.model_probability * 100:>10.2f}{fair}"
                    f"{item.expected_value * 100:>8.2f}%  {item.decision}"
                )

        print("\n" + "=" * 78)
        print("Готово. API: uvicorn app.main:app --reload  ->  GET /poc/report")


if __name__ == "__main__":
    main()
