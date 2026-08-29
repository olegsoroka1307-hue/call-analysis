"""FastAPI endpoints (ТЗ §52 п.9, §45)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sqlalchemy import select

from app.config import get_settings
from app.db.base import get_session
from app.db.models import Fixture
from app.main import app
from app.services.pricing import price_fixture
from app.providers.replay import build_replay_provider
from app.services.ingest import IngestResult, ingest_poll


@pytest.fixture
def client(session, replay_data_dir):
    provider, transport = build_replay_provider(replay_data_dir)
    result = IngestResult()
    transport.seek(0)
    while True:
        ingest_poll(session, provider, "soccer_epl", ["totals", "team_totals"],
                    transport.data_source, result)
        if not transport.advance():
            break

    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


class TestHealth:
    def test_reports_database_and_last_odds_update(self, client):
        """ТЗ §44: /health показує БД, провайдера і свіжість коефіцієнтів."""
        payload = client.get("/health").json()
        assert payload["status"] == "ok"
        assert payload["database"] is True
        assert payload["last_odds_update"] is not None
        assert payload["snapshots"] > 0


class TestFixtures:
    def test_lists_five_fixtures(self, client):
        payload = client.get("/fixtures").json()
        assert payload["count"] == 5
        assert len(payload["fixtures"]) == 5

    def test_synthetic_data_is_flagged_in_every_response(self, client):
        """ТЗ §1: синтетику неможливо сплутати з реальним ринком."""
        payload = client.get("/fixtures").json()
        assert payload["meta"]["data_source"] == "SYNTHETIC_REPLAY"
        assert "СИНТЕТИЧНІ" in payload["meta"]["warning"].upper()


class TestMovement:
    def test_returns_opening_current_and_closing(self, client):
        fixture_id = client.get("/fixtures").json()["fixtures"][0]["id"]
        movements = client.get(f"/fixtures/{fixture_id}/movement").json()["movements"]
        assert movements
        first = movements[0]
        assert {"opening", "current", "closing", "signal"} <= set(first)
        assert first["snapshot_count"] >= 3

    def test_filters_by_bookmaker(self, client):
        fixture_id = client.get("/fixtures").json()["fixtures"][0]["id"]
        movements = client.get(
            f"/fixtures/{fixture_id}/movement", params={"bookmaker": "pinnacle"}
        ).json()["movements"]
        assert movements
        assert {m["bookmaker"] for m in movements} == {"pinnacle"}

    def test_unknown_fixture_returns_404(self, client):
        assert client.get("/fixtures/999999/movement").status_code == 404


class TestMarkets:
    def test_prices_totals_and_team_totals(self, client):
        fixture_id = client.get("/fixtures").json()["fixtures"][0]["id"]
        payload = client.get(f"/fixtures/{fixture_id}/markets").json()
        codes = {m["market_code"] for m in payload["markets"]}
        assert {"TOTALS", "TEAM_TOTALS"} <= codes

    def test_every_market_carries_the_full_chain(self, client):
        """ТЗ §52 п.6-8: ринкова %, модельна %, fair odds, EV, рішення."""
        fixture_id = client.get("/fixtures").json()["fixtures"][0]["id"]
        for market in client.get(f"/fixtures/{fixture_id}/markets").json()["markets"]:
            assert market["market_probability"] is not None
            assert 0.0 < market["model_probability"] < 1.0
            assert market["fair_odds"] > 1.0
            assert market["decision"] in {"BET", "WATCH", "PASS", "REVERSE"}

    def test_reports_test_lambda_used(self, client):
        fixture_id = client.get("/fixtures").json()["fixtures"][0]["id"]
        model = client.get(f"/fixtures/{fixture_id}/markets").json()["model"]
        assert model["lambda_total"] == pytest.approx(
            model["lambda_home"] + model["lambda_away"]
        )


class TestPocReport:
    def test_covers_every_step_of_section_52(self, client):
        steps = client.get("/poc/report").json()["steps"]
        assert steps["2_fixtures_loaded"] == 5
        assert steps["3_storage"] == "PostgreSQL"
        assert steps["4_odds_snapshots"] >= 3
        assert steps["5_movement"]
        assert steps["6_7_8_pricing"]["totals"]
        assert steps["6_7_8_pricing"]["team_totals"]

    def test_includes_team_total_over_25(self, client):
        """ТЗ §52 п.7 вимагає саме team O2.5."""
        pricing = client.get("/poc/report").json()["steps"]["6_7_8_pricing"]
        lines = {
            m["line"] for m in pricing["team_totals"] if m["selection"] == "HOME_OVER"
        }
        assert 2.5 in lines


class TestValueCandidates:
    @pytest.fixture
    def with_persisted_predictions(self, client, session):
        """/predictions/value читає збережені predictions, тому їх треба записати."""
        settings = get_settings()
        for fixture in session.scalars(select(Fixture)):
            price_fixture(
                session, fixture,
                lambda_home=settings.poc_lambda_home,
                lambda_away=settings.poc_lambda_away,
                model_version=settings.model_version,
            )
        return client

    def test_returns_candidates_once_predictions_exist(self, with_persisted_predictions):
        payload = with_persisted_predictions.get(
            "/predictions/value", params={"min_ev": 0.02}
        ).json()
        assert payload["count"] > 0

    def test_only_returns_markets_above_threshold(self, with_persisted_predictions):
        payload = with_persisted_predictions.get(
            "/predictions/value", params={"min_ev": 0.02}
        ).json()
        assert payload["candidates"]
        for candidate in payload["candidates"]:
            assert candidate["expected_value_pct"] >= 2.0

    def test_higher_threshold_narrows_the_list(self, with_persisted_predictions):
        low = with_persisted_predictions.get(
            "/predictions/value", params={"min_ev": 0.02}
        ).json()["count"]
        high = with_persisted_predictions.get(
            "/predictions/value", params={"min_ev": 0.20}
        ).json()["count"]
        assert high <= low

    def test_empty_database_explains_what_to_run(self, session):
        """Порожня база має підказувати наступний крок, а не мовчати."""
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as bare_client:
            payload = bare_client.get("/predictions/value").json()
        app.dependency_overrides.clear()
        assert payload["count"] == 0
        assert "run_poc" in payload["hint"]
