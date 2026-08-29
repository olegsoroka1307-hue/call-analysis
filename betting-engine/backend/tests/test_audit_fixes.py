"""Тести під виправлення технічного аудиту від 29.08.2026.

Кожен клас нижче відповідає пункту аудиту і перевіряє саме той сценарій,
на якому стара поведінка давала неправильний результат. Тести написані так,
щоб вони падали на старому коді — інакше вони нічого не стережуть.

Див. docs/AUDIT-2026-08-29.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.base import get_session
from app.db.models import Bookmaker, Fixture, League, MarketPrediction, ModelRun, OddsSnapshot, Team
from app.main import app
from app.models_engine.decision import (
    Decision,
    decide,
    decision_thresholds,
    disagreement_reason_codes,
    evaluate_data_guard,
)
from app.services.ingest import IngestResult, insert_odds_snapshots
from app.services.pricing import price_fixture, recompute_from_inputs
from app.providers.base import ProviderOdds

KICKOFF = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
LAMBDA_HOME, LAMBDA_AWAY = 1.55, 1.20
MODEL_VERSION = "test-0.1"


# ---------------------------------------------------------------------------
# Пункт 4 — межа market disagreement
# ---------------------------------------------------------------------------

class TestDisagreementBoundary:
    """Аудит п.4: рівно на 0.07 код давав BET і REQUIRE_DEEP_REVIEW одночасно."""

    def test_exactly_at_threshold_is_not_bet(self):
        threshold = decision_thresholds()["disagreement_deep_review"]
        assert decide(0.10, disagreement=threshold) is Decision.WATCH

    def test_just_below_threshold_stays_bet(self):
        threshold = decision_thresholds()["disagreement_deep_review"]
        assert decide(0.10, disagreement=threshold - 1e-9) is Decision.BET

    def test_decision_and_reason_codes_agree_on_the_boundary(self):
        """Рішення і причини не можуть суперечити одне одному на тому самому числі."""
        threshold = decision_thresholds()["disagreement_deep_review"]
        decision = decide(0.10, disagreement=threshold)
        codes = disagreement_reason_codes(threshold)
        assert "REQUIRE_DEEP_REVIEW" in codes
        assert decision is not Decision.BET


# ---------------------------------------------------------------------------
# Пункт 3 — захист від протухлих і неповних даних
# ---------------------------------------------------------------------------

class TestDataGuard:
    """Аудит п.3: odds_age_seconds лише друкувався, а BET проходив попри все."""

    def test_missing_opposite_side_forces_pass(self):
        guard = evaluate_data_guard(market_probability=None, odds_age_seconds=10.0)
        assert guard.forced is Decision.PASS
        assert "DATA_UNAVAILABLE" in guard.reason_codes
        # Навіть величезний EV не робить це ставкою.
        assert decide(0.50, disagreement=None, guard=guard) is Decision.PASS

    def test_stale_odds_downgrade_to_watch(self):
        t = decision_thresholds()
        guard = evaluate_data_guard(
            market_probability=0.5, odds_age_seconds=t["stale_watch_seconds"] + 1
        )
        assert "STALE_ODDS_WATCH" in guard.reason_codes
        assert decide(0.10, disagreement=0.0, guard=guard) is Decision.WATCH

    def test_very_stale_odds_force_pass(self):
        t = decision_thresholds()
        guard = evaluate_data_guard(
            market_probability=0.5, odds_age_seconds=t["stale_pass_seconds"] + 1
        )
        assert "STALE_ODDS_PASS" in guard.reason_codes
        assert decide(0.50, disagreement=0.0, guard=guard) is Decision.PASS

    def test_future_timestamp_is_provider_error(self):
        guard = evaluate_data_guard(market_probability=0.5, odds_age_seconds=-3600.0)
        assert "PROVIDER_ERROR_FUTURE_TIMESTAMP" in guard.reason_codes
        assert guard.forced is Decision.PASS

    def test_small_clock_skew_is_tolerated(self):
        """Розсинхрон на секунду — не помилка провайдера."""
        guard = evaluate_data_guard(market_probability=0.5, odds_age_seconds=-1.0)
        assert guard.forced is None
        assert not guard.reason_codes

    def test_snapshot_after_kickoff_forces_pass(self):
        guard = evaluate_data_guard(
            market_probability=0.5, odds_age_seconds=10.0, snapshot_after_kickoff=True
        )
        assert "SNAPSHOT_AFTER_KICKOFF" in guard.reason_codes
        assert guard.forced is Decision.PASS

    def test_fresh_complete_data_does_not_block(self):
        guard = evaluate_data_guard(market_probability=0.5, odds_age_seconds=30.0)
        assert guard.forced is None
        assert decide(0.10, disagreement=0.0, guard=guard) is Decision.BET


# ---------------------------------------------------------------------------
# Фікстури для тестів з базою
# ---------------------------------------------------------------------------

@pytest.fixture
def fixture_row(session):
    league = League(provider="test", provider_id="test_league", name="Test League")
    session.add(league)
    session.flush()
    home = Team(provider="test", name="Home FC", league_id=league.id)
    away = Team(provider="test", name="Away FC", league_id=league.id)
    session.add_all([home, away])
    session.flush()
    fixture = Fixture(
        provider="test",
        provider_fixture_id="fx-1",
        league_id=league.id,
        home_team_id=home.id,
        away_team_id=away.id,
        kickoff_at=KICKOFF,
        status="NS",
        data_source="LIVE",
    )
    session.add(fixture)
    session.flush()
    return fixture


@pytest.fixture
def bookmaker_row(session):
    bookmaker = Bookmaker(key="stake", name="Stake", is_sharp=False, is_preferred=True)
    session.add(bookmaker)
    session.flush()
    return bookmaker


def add_snapshot(session, fixture, bookmaker, selection, line, odds, minutes_before_kickoff,
                 data_source="LIVE"):
    snapshot = OddsSnapshot(
        fixture_id=fixture.id,
        bookmaker_id=bookmaker.id,
        market_code="TOTALS",
        selection=selection,
        line=line,
        odds=odds,
        data_source=data_source,
        source_timestamp=KICKOFF - timedelta(minutes=minutes_before_kickoff),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


# ---------------------------------------------------------------------------
# Пункт 1 — оцінюється лише актуальна лінія
# ---------------------------------------------------------------------------

class TestCurrentLineOnly:
    """Аудит п.1: після руху 2.5 -> 2.75 стара лінія лишалась «доступним ринком»."""

    def test_retired_line_is_not_priced(self, session, fixture_row, bookmaker_row):
        # Ринок стояв на 2.5, потім переїхав на 2.75.
        add_snapshot(session, fixture_row, bookmaker_row, "OVER", 2.5, 1.90, 120)
        add_snapshot(session, fixture_row, bookmaker_row, "UNDER", 2.5, 1.95, 120)
        add_snapshot(session, fixture_row, bookmaker_row, "OVER", 2.75, 2.00, 30)
        add_snapshot(session, fixture_row, bookmaker_row, "UNDER", 2.75, 1.85, 30)
        session.commit()

        _run, priced = price_fixture(
            session, fixture_row, LAMBDA_HOME, LAMBDA_AWAY, MODEL_VERSION,
            persist=False, now=KICKOFF - timedelta(minutes=25),
        )
        lines = {item.line for item in priced}
        assert lines == {2.75}, f"знята лінія 2.5 не має оцінюватись, отримали {lines}"

    def test_both_sides_of_current_line_are_priced(self, session, fixture_row, bookmaker_row):
        add_snapshot(session, fixture_row, bookmaker_row, "OVER", 2.75, 2.00, 30)
        add_snapshot(session, fixture_row, bookmaker_row, "UNDER", 2.75, 1.85, 30)
        session.commit()

        _run, priced = price_fixture(
            session, fixture_row, LAMBDA_HOME, LAMBDA_AWAY, MODEL_VERSION,
            persist=False, now=KICKOFF - timedelta(minutes=25),
        )
        assert {item.selection for item in priced} == {"OVER", "UNDER"}
        # Обидві сторони на місці -> no-vig порахований.
        assert all(item.market_probability is not None for item in priced)

    def test_side_left_behind_on_old_line_loses_no_vig(
        self, session, fixture_row, bookmaker_row
    ):
        """Якщо одна сторона не переїхала — це не «майже повний ринок», а неповний."""
        add_snapshot(session, fixture_row, bookmaker_row, "OVER", 2.5, 1.90, 120)
        add_snapshot(session, fixture_row, bookmaker_row, "UNDER", 2.5, 1.95, 120)
        add_snapshot(session, fixture_row, bookmaker_row, "OVER", 2.75, 2.00, 30)
        session.commit()

        _run, priced = price_fixture(
            session, fixture_row, LAMBDA_HOME, LAMBDA_AWAY, MODEL_VERSION,
            persist=False, now=KICKOFF - timedelta(minutes=25),
        )
        assert [item.selection for item in priced] == ["OVER"]
        item = priced[0]
        assert item.line == 2.75
        assert item.market_probability is None
        assert "NO_VIG_UNAVAILABLE_MISSING_OPPOSITE_SIDE" in item.reason_codes
        assert item.decision == Decision.PASS.value

    def test_snapshot_after_kickoff_is_not_used_for_bet(
        self, session, fixture_row, bookmaker_row
    ):
        add_snapshot(session, fixture_row, bookmaker_row, "OVER", 2.5, 2.00, -30)   # після kickoff
        add_snapshot(session, fixture_row, bookmaker_row, "UNDER", 2.5, 1.85, -30)
        session.commit()

        _run, priced = price_fixture(
            session, fixture_row, LAMBDA_HOME, LAMBDA_AWAY, MODEL_VERSION,
            persist=False, now=KICKOFF + timedelta(minutes=35),
        )
        assert priced, "рядок має лишитись видимим, а не тихо зникнути"
        for item in priced:
            assert "SNAPSHOT_AFTER_KICKOFF" in item.reason_codes
            assert item.decision == Decision.PASS.value


# ---------------------------------------------------------------------------
# Пункт 2 — ingest без фільтра по лінії
# ---------------------------------------------------------------------------

class TestSnapshotIngest:
    """Аудит п.2: 2.5 -> 2.75 -> 2.5 з тією ж ціною губило повернення лінії."""

    def _odds(self, selection: str, line: float, odds: float, minute: int) -> ProviderOdds:
        return ProviderOdds(
            provider_event_id="fx-1",
            bookmaker_key="stake",
            bookmaker_title="Stake",
            market_code="TOTALS",
            selection=selection,
            line=line,
            odds=odds,
            source_timestamp=KICKOFF - timedelta(minutes=minute),
        )

    def test_line_returning_to_previous_value_is_recorded(
        self, session, fixture_row, bookmaker_row
    ):
        fixtures = {"fx-1": fixture_row}
        result = IngestResult()
        for line, minute in ((2.5, 120), (2.75, 90), (2.5, 60)):
            insert_odds_snapshots(
                session, [self._odds("OVER", line, 1.90, minute)], fixtures, result
            )
            session.commit()

        rows = list(
            session.scalars(
                select(OddsSnapshot)
                .where(OddsSnapshot.selection == "OVER")
                .order_by(OddsSnapshot.source_timestamp)
            )
        )
        assert [r.line for r in rows] == [2.5, 2.75, 2.5]
        assert result.snapshots_inserted == 3
        assert result.snapshots_skipped_unchanged == 0

    def test_truly_unchanged_price_is_still_skipped(
        self, session, fixture_row, bookmaker_row
    ):
        """Дедуплікація має лишитись: append-only не означає «пиши все підряд»."""
        fixtures = {"fx-1": fixture_row}
        result = IngestResult()
        insert_odds_snapshots(session, [self._odds("OVER", 2.5, 1.90, 120)], fixtures, result)
        session.commit()
        insert_odds_snapshots(session, [self._odds("OVER", 2.5, 1.90, 90)], fixtures, result)
        session.commit()

        assert result.snapshots_inserted == 1
        assert result.snapshots_skipped_unchanged == 1

    def test_data_source_is_written_on_the_snapshot(
        self, session, fixture_row, bookmaker_row
    ):
        fixtures = {"fx-1": fixture_row}
        result = IngestResult()
        insert_odds_snapshots(
            session, [self._odds("OVER", 2.5, 1.90, 120)], fixtures, result,
            data_source="SYNTHETIC_REPLAY",
        )
        session.commit()
        row = session.scalars(select(OddsSnapshot)).first()
        assert row.data_source == "SYNTHETIC_REPLAY"


# ---------------------------------------------------------------------------
# Пункт 5 — повна відтворюваність з inputs_json
# ---------------------------------------------------------------------------

@pytest.fixture
def persisted_run(session, fixture_row, bookmaker_row):
    add_snapshot(session, fixture_row, bookmaker_row, "OVER", 2.75, 2.00, 30)
    add_snapshot(session, fixture_row, bookmaker_row, "UNDER", 2.75, 1.85, 30)
    session.commit()
    model_run, priced = price_fixture(
        session, fixture_row, LAMBDA_HOME, LAMBDA_AWAY, MODEL_VERSION,
        now=KICKOFF - timedelta(minutes=25),
    )
    return model_run, priced


class TestReproducibilityFromInputsOnly:
    """Аудит п.5: старий тест брав selection/line/odds з самої prediction.

    Тобто перевіряв «числа збігаються з тими самими числами». Тут єдиний
    вхід — inputs_json і append-only снапшоти, на які він посилається.
    """

    def test_inputs_json_carries_everything_needed(self, persisted_run):
        model_run, _priced = persisted_run
        inputs = model_run.inputs_json
        required = {
            "schema_version", "model_version", "lambda_home", "lambda_away",
            "max_goals", "snapshot_ids", "prediction_cutoff",
            "decision_thresholds", "market_mapping", "feature_flags", "inputs_hash",
        }
        missing = required - set(inputs)
        assert not missing, f"в inputs_json бракує: {sorted(missing)}"
        assert inputs["model_version"] == MODEL_VERSION
        assert inputs["snapshot_ids"]

    def test_every_field_of_every_prediction_is_reproduced(self, session, persisted_run):
        model_run, _priced = persisted_run
        recomputed = recompute_from_inputs(session, model_run.inputs_json)
        stored = list(
            session.scalars(
                select(MarketPrediction).where(
                    MarketPrediction.model_run_id == model_run.id
                )
            )
        )
        assert stored, "прогін не зберіг жодної prediction"
        assert len(recomputed) == len(stored)

        by_key = {
            (item.bookmaker, item.market_code, item.selection, item.line): item
            for item in recomputed
        }
        bookmakers = {b.id: b.key for b in session.scalars(select(Bookmaker))}

        for prediction in stored:
            key = (
                bookmakers[prediction.bookmaker_id],
                prediction.market_code,
                prediction.selection,
                prediction.line,
            )
            assert key in by_key, f"перерахунок не відтворив {key}"
            item = by_key[key]

            assert item.bookmaker_odds == pytest.approx(prediction.bookmaker_odds)
            assert item.model_probability == pytest.approx(prediction.model_probability)
            assert item.expected_value == pytest.approx(prediction.expected_value)
            assert item.decision == prediction.decision

            for attribute, column in (
                ("market_probability", prediction.market_probability),
                ("fair_odds", prediction.fair_odds),
            ):
                value = getattr(item, attribute)
                if column is None:
                    assert value is None
                else:
                    assert value == pytest.approx(column)

            stored_codes = prediction.reason_codes_json["codes"]
            assert item.reason_codes == stored_codes

            stored_edge = prediction.reason_codes_json.get("edge")
            if stored_edge is None:
                assert item.edge is None
            else:
                assert item.edge == pytest.approx(stored_edge)

            stored_disagreement = prediction.reason_codes_json.get("market_disagreement")
            if stored_disagreement is None:
                assert item.market_disagreement is None
            else:
                assert item.market_disagreement == pytest.approx(stored_disagreement)

    def test_recompute_uses_recorded_thresholds_not_current_ones(
        self, session, persisted_run
    ):
        """Зміна порогів не має заднім числом переписувати старі рішення."""
        model_run, _priced = persisted_run
        inputs = dict(model_run.inputs_json)
        # Робимо пороги свідомо недосяжними — усе має стати PASS.
        inputs["decision_thresholds"] = {
            **inputs["decision_thresholds"],
            "pass_below": 9.99,
        }
        recomputed = recompute_from_inputs(session, inputs)
        assert recomputed
        assert {item.decision for item in recomputed} == {Decision.PASS.value}

    def test_inputs_hash_changes_when_inputs_change(self, persisted_run):
        model_run, _priced = persisted_run
        from app.services.pricing import build_inputs_json

        base = model_run.inputs_json
        other = build_inputs_json(
            lambda_home=LAMBDA_HOME + 0.1,
            lambda_away=LAMBDA_AWAY,
            max_goals=base["max_goals"],
            model_version=base["model_version"],
            snapshot_ids=base["snapshot_ids"],
            prediction_cutoff=datetime.fromisoformat(base["prediction_cutoff"]),
            data_source=base["data_source"],
        )
        assert other["inputs_hash"] != base["inputs_hash"]


# ---------------------------------------------------------------------------
# Пункти 6, 7, 8 — API
# ---------------------------------------------------------------------------

@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestValueCandidatesEndpoint:
    """Аудит п.6: latest_run рахувався і не використовувався."""

    def test_only_latest_run_is_returned(
        self, session, client, fixture_row, bookmaker_row
    ):
        add_snapshot(session, fixture_row, bookmaker_row, "OVER", 2.75, 2.00, 30)
        add_snapshot(session, fixture_row, bookmaker_row, "UNDER", 2.75, 1.85, 30)
        session.commit()

        for _ in range(3):
            price_fixture(
                session, fixture_row, LAMBDA_HOME, LAMBDA_AWAY, MODEL_VERSION,
                now=KICKOFF - timedelta(minutes=25),
            )
        runs = list(session.scalars(select(ModelRun.id)))
        assert len(runs) == 3, "тест має сенс лише за кількох прогонів"

        payload = client.get("/predictions/value", params={"min_ev": -1.0}).json()
        run_ids = {c["model_run_id"] for c in payload["candidates"]}
        assert run_ids <= {max(runs)}, "у видачі є кандидати зі старих прогонів"

    def test_no_duplicate_candidates(self, session, client, fixture_row, bookmaker_row):
        add_snapshot(session, fixture_row, bookmaker_row, "OVER", 2.75, 2.00, 30)
        add_snapshot(session, fixture_row, bookmaker_row, "UNDER", 2.75, 1.85, 30)
        session.commit()
        for _ in range(3):
            price_fixture(
                session, fixture_row, LAMBDA_HOME, LAMBDA_AWAY, MODEL_VERSION,
                now=KICKOFF - timedelta(minutes=25),
            )

        payload = client.get("/predictions/value", params={"min_ev": -1.0}).json()
        keys = [
            (c["match"], c["bookmaker"], c["market"], c["selection"], c["line"])
            for c in payload["candidates"]
        ]
        assert len(keys) == len(set(keys)), f"дублікати у видачі: {keys}"

    def test_pass_decisions_never_appear(self, session, client, fixture_row, bookmaker_row):
        # Немає протилежної сторони -> guard робить PASS.
        add_snapshot(session, fixture_row, bookmaker_row, "OVER", 2.75, 5.00, 30)
        session.commit()
        price_fixture(
            session, fixture_row, LAMBDA_HOME, LAMBDA_AWAY, MODEL_VERSION,
            now=KICKOFF - timedelta(minutes=25),
        )
        payload = client.get("/predictions/value", params={"min_ev": -1.0}).json()
        assert all(c["decision"] != "PASS" for c in payload["candidates"])


class TestProvenanceIsPerRow:
    """Аудит п.7: прапорець «синтетика» рахувався глобально по всій базі."""

    def test_live_only_fixture_has_no_synthetic_warning(
        self, session, client, fixture_row, bookmaker_row
    ):
        add_snapshot(session, fixture_row, bookmaker_row, "OVER", 2.75, 2.00, 30)
        add_snapshot(session, fixture_row, bookmaker_row, "UNDER", 2.75, 1.85, 30)
        session.commit()
        payload = client.get(f"/fixtures/{fixture_row.id}/markets").json()
        assert payload["meta"] is None

    def test_synthetic_row_elsewhere_does_not_taint_live_fixture(
        self, session, client, fixture_row, bookmaker_row
    ):
        """Головний сценарій аудиту: у змішаній базі позначка мусить лишатись точною."""
        synthetic = Fixture(
            provider="test",
            provider_fixture_id="fx-synthetic",
            league_id=fixture_row.league_id,
            home_team_id=fixture_row.home_team_id,
            away_team_id=fixture_row.away_team_id,
            kickoff_at=KICKOFF,
            status="NS",
            data_source="SYNTHETIC_REPLAY",
        )
        session.add(synthetic)
        add_snapshot(session, fixture_row, bookmaker_row, "OVER", 2.75, 2.00, 30)
        add_snapshot(session, fixture_row, bookmaker_row, "UNDER", 2.75, 1.85, 30)
        session.commit()

        payload = client.get(f"/fixtures/{fixture_row.id}/markets").json()
        assert payload["meta"] is None, (
            "живий матч позначений синтетикою через сторонній рядок у базі"
        )

    def test_synthetic_fixture_is_flagged(self, session, client, bookmaker_row, fixture_row):
        fixture_row.data_source = "SYNTHETIC_REPLAY"
        add_snapshot(session, fixture_row, bookmaker_row, "OVER", 2.75, 2.00, 30,
                     data_source="SYNTHETIC_REPLAY")
        add_snapshot(session, fixture_row, bookmaker_row, "UNDER", 2.75, 1.85, 30,
                     data_source="SYNTHETIC_REPLAY")
        session.commit()
        payload = client.get(f"/fixtures/{fixture_row.id}/markets").json()
        assert payload["meta"] is not None
        assert payload["meta"]["data_source"] == "SYNTHETIC_REPLAY"

    def test_model_run_records_its_own_provenance(self, session, persisted_run):
        model_run, _priced = persisted_run
        assert model_run.data_source == "LIVE"


class TestHealthDegradesInsteadOfCrashing:
    """Аудит п.8: два DB-запити стояли поза try, тому /health віддавав 500."""

    def test_health_returns_degraded_when_db_is_down(self):
        class DeadSession:
            def execute(self, *args, **kwargs):
                raise RuntimeError("database is gone")

            def scalar(self, *args, **kwargs):
                raise RuntimeError("database is gone")

        app.dependency_overrides[get_session] = lambda: DeadSession()
        try:
            with TestClient(app) as client:
                response = client.get("/health")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "degraded"
        assert payload["database"] is False
        assert payload["database_error"] == "RuntimeError"

    def test_health_is_ok_on_live_db(self, client):
        payload = client.get("/health").json()
        assert payload["status"] == "ok"
        assert payload["database"] is True
        assert payload["database_error"] is None
