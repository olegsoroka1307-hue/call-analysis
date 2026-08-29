"""Завантаження fixtures та odds snapshots у PostgreSQL (ТЗ §52 п.3-4)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Bookmaker, Fixture, League, OddsSnapshot, Team
from ..providers.base import OddsProvider, ProviderEvent, ProviderOdds

PREFERRED_BOOKMAKER_KEYS = {"stake"}
SHARP_BOOKMAKER_KEYS = {"pinnacle"}

#: Простір імен ідентифікаторів. Не плутати з data_source: replay-датасет
#: лежить у форматі The Odds API, тому namespace той самий, а походження — ні.
DEFAULT_PROVIDER = "the_odds_api"


@dataclass
class IngestResult:
    fixtures_created: int = 0
    fixtures_seen: int = 0
    snapshots_inserted: int = 0
    snapshots_skipped_unchanged: int = 0

    def __str__(self) -> str:
        return (
            f"fixtures: {self.fixtures_seen} seen / {self.fixtures_created} new; "
            f"snapshots: {self.snapshots_inserted} inserted / "
            f"{self.snapshots_skipped_unchanged} unchanged"
        )


def _get_or_create_league(
    session: Session, provider: str, provider_id: str, name: str
) -> League:
    league = session.scalar(
        select(League).where(League.provider == provider, League.provider_id == provider_id)
    )
    if league is None:
        league = League(
            provider=provider, provider_id=provider_id, name=name, country="England"
        )
        session.add(league)
        session.flush()
    return league


def _get_or_create_team(
    session: Session, provider: str, name: str, league_id: int
) -> Team:
    team = session.scalar(
        select(Team).where(Team.provider == provider, Team.name == name)
    )
    if team is None:
        team = Team(provider=provider, name=name, league_id=league_id)
        session.add(team)
        session.flush()
    return team


def _get_or_create_bookmaker(session: Session, key: str, title: str) -> Bookmaker:
    bookmaker = session.scalar(select(Bookmaker).where(Bookmaker.key == key))
    if bookmaker is None:
        bookmaker = Bookmaker(
            key=key,
            name=title,
            is_sharp=key in SHARP_BOOKMAKER_KEYS,
            is_preferred=key in PREFERRED_BOOKMAKER_KEYS,
        )
        session.add(bookmaker)
        session.flush()
    return bookmaker


def upsert_fixtures(
    session: Session,
    events: list[ProviderEvent],
    data_source: str,
    result: IngestResult,
    provider: str = DEFAULT_PROVIDER,
) -> dict[str, Fixture]:
    fixtures: dict[str, Fixture] = {}
    for event in events:
        result.fixtures_seen += 1
        league = _get_or_create_league(
            session, provider, event.sport_key, event.league_name
        )
        home = _get_or_create_team(session, provider, event.home_team, league.id)
        away = _get_or_create_team(session, provider, event.away_team, league.id)

        fixture = session.scalar(
            select(Fixture).where(
                Fixture.provider == provider,
                Fixture.provider_fixture_id == event.provider_event_id,
            )
        )
        if fixture is None:
            fixture = Fixture(
                provider=provider,
                provider_fixture_id=event.provider_event_id,
                league_id=league.id,
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_at=event.commence_time,
                status="NS",
                data_source=data_source,
            )
            session.add(fixture)
            session.flush()
            result.fixtures_created += 1
        else:
            # Час старту може зсуватися — це нормальний апдейт.
            fixture.kickoff_at = event.commence_time
            # ТЗ §1: якщо матч уже бачили в replay, а тепер він приходить з
            # живого провайдера (або навпаки) — походження мусить це показати,
            # інакше стара позначка тихо бреше про свіжі дані.
            if fixture.data_source != data_source:
                fixture.data_source = (
                    data_source if fixture.data_source is None else "MIXED"
                )
        fixtures[event.provider_event_id] = fixture
    return fixtures


def insert_odds_snapshots(
    session: Session,
    odds: list[ProviderOdds],
    fixtures: dict[str, Fixture],
    result: IngestResult,
    data_source: str = "LIVE",
) -> None:
    """Append-only запис коефіцієнтів (ТЗ §7.2).

    Рядок додається лише тоді, коли ціна **або лінія** відрізняються від
    останнього стану цієї селекції — «кожна зміна = INSERT».

    Останній стан шукається БЕЗ фільтра по лінії. Інакше послідовність
    2.5 -> 2.75 -> 2.5 з тією ж ціною виглядала б як «без змін» відносно
    старого рядка 2.5, і повернення лінії на 2.5 не потрапило б у базу.

    Наявні рядки ніколи не оновлюються (це ще й заборонено тригером у БД).
    """
    for row in odds:
        fixture = fixtures.get(row.provider_event_id)
        if fixture is None:
            continue  # ціна на матч, якого немає у вибірці
        bookmaker = _get_or_create_bookmaker(session, row.bookmaker_key, row.bookmaker_title)

        last = session.scalar(
            select(OddsSnapshot)
            .where(
                OddsSnapshot.fixture_id == fixture.id,
                OddsSnapshot.bookmaker_id == bookmaker.id,
                OddsSnapshot.market_code == row.market_code,
                OddsSnapshot.selection == row.selection,
            )
            .order_by(OddsSnapshot.source_timestamp.desc(), OddsSnapshot.id.desc())
            .limit(1)
        )
        if last is not None and last.odds == row.odds and last.line == row.line:
            result.snapshots_skipped_unchanged += 1
            continue

        session.add(
            OddsSnapshot(
                fixture_id=fixture.id,
                bookmaker_id=bookmaker.id,
                market_code=row.market_code,
                selection=row.selection,
                line=row.line,
                odds=row.odds,
                data_source=data_source,
                source_timestamp=row.source_timestamp,
            )
        )
        result.snapshots_inserted += 1


def ingest_poll(
    session: Session,
    provider: OddsProvider,
    sport_key: str,
    markets: list[str],
    data_source: str,
    result: IngestResult | None = None,
    provider_key: str = DEFAULT_PROVIDER,
) -> IngestResult:
    """Одне опитування провайдера: матчі + коефіцієнти -> БД."""
    result = result or IngestResult()
    events = provider.get_events(sport_key)
    fixtures = upsert_fixtures(session, events, data_source, result, provider_key)
    odds = provider.get_odds(sport_key, markets)
    insert_odds_snapshots(session, odds, fixtures, result, data_source)
    session.commit()
    return result
