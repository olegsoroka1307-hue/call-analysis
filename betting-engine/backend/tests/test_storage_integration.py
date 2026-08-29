"""Інтеграція: провайдер -> БД -> snapshots -> movement (ТЗ §7.2, §28, §46.2)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.db.models import Bookmaker, Fixture, OddsSnapshot
from app.providers.replay import build_replay_provider
from app.services.ingest import IngestResult, ingest_poll
from app.services.movement import get_movement

MARKETS = ["totals", "team_totals"]
SPORT = "soccer_epl"


@pytest.fixture
def ingested(session, replay_data_dir):
    """Прогін усіх опитувань реплею в базу — як робить run_poc."""
    provider, transport = build_replay_provider(replay_data_dir)
    result = IngestResult()
    transport.seek(0)
    while True:
        ingest_poll(session, provider, SPORT, MARKETS, transport.data_source, result)
        if not transport.advance():
            break
    return result


class TestIngest:
    def test_loads_five_fixtures(self, session, ingested):
        """ТЗ §52 п.2-3: 5 матчів збережено у PostgreSQL."""
        assert session.scalar(select(func.count()).select_from(Fixture)) == 5
        assert ingested.fixtures_created == 5

    def test_fixtures_are_not_duplicated_across_polls(self, session, ingested):
        """5 опитувань х 5 матчів = 25 побачених, але лише 5 рядків."""
        assert ingested.fixtures_seen == 25
        assert session.scalar(select(func.count()).select_from(Fixture)) == 5

    def test_data_source_is_recorded_on_every_fixture(self, session, ingested):
        """ТЗ §1: походження даних не має губитися."""
        sources = set(session.scalars(select(Fixture.data_source).distinct()))
        assert sources == {"SYNTHETIC_REPLAY"}

    def test_stores_three_to_five_snapshots_per_total_market(self, session, ingested):
        """ТЗ §52 п.4: 3-5 odds snapshots для одного тотального ринку.

        Рахуємо в розрізі одного букмекера: у датасеті їх два, і кожен веде
        власну історію цін.
        """
        fixture = session.scalars(select(Fixture).order_by(Fixture.kickoff_at)).first()
        for bookmaker in session.scalars(select(Bookmaker)):
            count = session.scalar(
                select(func.count()).select_from(OddsSnapshot).where(
                    OddsSnapshot.fixture_id == fixture.id,
                    OddsSnapshot.bookmaker_id == bookmaker.id,
                    OddsSnapshot.market_code == "TOTALS",
                    OddsSnapshot.selection == "OVER",
                )
            )
            assert 3 <= count <= 5, f"{bookmaker.key}: {count} snapshots"

    def test_unchanged_price_is_not_stored_twice(self, session, ingested):
        """ТЗ §7.2: кожна ЗМІНА = INSERT, повтор тієї ж ціни рядка не створює."""
        assert ingested.snapshots_skipped_unchanged > 0


class TestAppendOnly:
    def test_update_is_rejected_by_database(self, session, ingested):
        """ТЗ §2, §7.2: історичні snapshots не перезаписуються."""
        snapshot = session.scalars(select(OddsSnapshot).limit(1)).one()
        snapshot.odds = 9.99
        with pytest.raises(DBAPIError, match="append-only"):
            session.flush()
        session.rollback()

    def test_delete_is_rejected_by_database(self, session, ingested):
        snapshot = session.scalars(select(OddsSnapshot).limit(1)).one()
        session.delete(snapshot)
        with pytest.raises(DBAPIError, match="append-only"):
            session.flush()
        session.rollback()


class TestMovement:
    @pytest.fixture
    def context(self, session, ingested):
        fixture = session.scalars(select(Fixture).order_by(Fixture.kickoff_at)).first()
        bookmaker = session.scalars(
            select(Bookmaker).where(Bookmaker.is_preferred.is_(True))
        ).first()
        return fixture, bookmaker

    def test_opening_is_first_and_current_is_last(self, session, context):
        """ТЗ §7.2: opening = перший валідний snapshot, current = останній."""
        fixture, bookmaker = context
        movement = get_movement(
            session, fixture.id, "TOTALS", "OVER",
            bookmaker.id, bookmaker.key, fixture.kickoff_at,
        )
        history = list(
            session.scalars(
                select(OddsSnapshot)
                .where(
                    OddsSnapshot.fixture_id == fixture.id,
                    OddsSnapshot.bookmaker_id == bookmaker.id,
                    OddsSnapshot.market_code == "TOTALS",
                    OddsSnapshot.selection == "OVER",
                )
                .order_by(OddsSnapshot.source_timestamp)
            )
        )
        assert movement.opening_odds == history[0].odds
        assert movement.current_odds == history[-1].odds
        assert movement.opening_at <= movement.current_at

    def test_closing_is_taken_strictly_before_kickoff(self, session, context):
        """ТЗ §7.2: closing = останній валідний snapshot СТРОГО до kickoff."""
        fixture, bookmaker = context
        movement = get_movement(
            session, fixture.id, "TOTALS", "OVER",
            bookmaker.id, bookmaker.key, fixture.kickoff_at,
        )
        assert movement.closing_odds is not None
        assert movement.current_at < fixture.kickoff_at

    def test_line_move_is_reported_separately_from_price_move(self, session, ingested):
        """ТЗ §28: рух ЛІНІЇ і рух ЦІНИ — різні сигнали."""
        moved = None
        bookmaker = session.scalars(select(Bookmaker)).first()
        for fixture in session.scalars(select(Fixture)):
            movement = get_movement(
                session, fixture.id, "TOTALS", "OVER",
                bookmaker.id, bookmaker.key, fixture.kickoff_at,
            )
            if movement and movement.line_move:
                moved = movement
                break
        assert moved is not None, "у датасеті має бути матч із рухом самої лінії"
        assert moved.signal in ("TOTAL_LINE_UP", "TOTAL_LINE_DOWN")
        assert moved.opening_line != moved.current_line

    def test_missing_market_returns_none_instead_of_guessing(self, session, context):
        """ТЗ §1: немає даних -> None, а не вигаданий рух."""
        fixture, bookmaker = context
        assert get_movement(
            session, fixture.id, "CORNERS", "OVER",
            bookmaker.id, bookmaker.key, fixture.kickoff_at,
        ) is None
