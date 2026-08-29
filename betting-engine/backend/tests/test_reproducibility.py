"""Відтворюваність prediction (ТЗ §51, §1).

Критерій приймання §51: «Prediction можна відтворити з model_run.inputs_json»
і «Неіснуючі дані не підміняються guesses».
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import Fixture, MarketPrediction, ModelRun, OddsSnapshot
from app.models_engine.ev import evaluate
from app.models_engine.poisson import build_score_matrix, outcome_distribution
from app.providers.base import ProviderError
from app.providers.replay import build_replay_provider
from app.providers.the_odds_api import HttpTransport
from app.services.ingest import IngestResult, ingest_poll
from app.services.pricing import SELECTION_SPEC, price_fixture


@pytest.fixture
def priced(session, replay_data_dir):
    provider, transport = build_replay_provider(replay_data_dir)
    result = IngestResult()
    transport.seek(0)
    while True:
        ingest_poll(session, provider, "soccer_epl", ["totals", "team_totals"],
                    transport.data_source, result)
        if not transport.advance():
            break
    settings = get_settings()
    fixture = session.scalars(select(Fixture).order_by(Fixture.kickoff_at)).first()
    model_run, _ = price_fixture(
        session, fixture,
        lambda_home=settings.poc_lambda_home,
        lambda_away=settings.poc_lambda_away,
        model_version=settings.model_version,
    )
    return session, model_run


class TestReproducibility:
    def test_inputs_json_records_everything_needed(self, priced):
        _session, model_run = priced
        inputs = model_run.inputs_json
        assert {"lambda_home", "lambda_away", "max_goals", "snapshot_ids"} <= set(inputs)
        assert inputs["snapshot_ids"]
        assert inputs["data_source"] == "SYNTHETIC_REPLAY"

    def test_prediction_can_be_recomputed_from_inputs_json(self, priced):
        """Перерахунок лише з inputs_json має дати ті самі числа."""
        session, model_run = priced
        inputs = model_run.inputs_json
        score_matrix = build_score_matrix(
            inputs["lambda_home"], inputs["lambda_away"], inputs["max_goals"]
        )
        predictions = list(
            session.scalars(
                select(MarketPrediction).where(
                    MarketPrediction.model_run_id == model_run.id
                )
            )
        )
        assert predictions

        for prediction in predictions:
            side, scope, _opposite = SELECTION_SPEC[prediction.selection]
            distribution = outcome_distribution(
                score_matrix, side, prediction.line, scope
            )
            recomputed = evaluate(distribution, prediction.bookmaker_odds)
            assert recomputed.model_probability == pytest.approx(
                prediction.model_probability
            )
            assert recomputed.expected_value == pytest.approx(prediction.expected_value)

    def test_referenced_snapshots_still_exist_unchanged(self, priced):
        """Snapshots append-only, тому вхід prediction не може «поїхати» заднім числом."""
        session, model_run = priced
        for snapshot_id in model_run.inputs_json["snapshot_ids"]:
            assert session.get(OddsSnapshot, snapshot_id) is not None

    def test_model_version_is_stored(self, priced):
        """ТЗ §1: кожна prediction має model_version."""
        _session, model_run = priced
        assert model_run.model_version == get_settings().model_version


class TestNoGuessing:
    def test_provider_without_api_key_fails_loudly(self):
        """ТЗ §1: немає ключа -> помилка, а не тихі вигадані дані."""
        with pytest.raises(ProviderError, match="ODDS_API_KEY"):
            HttpTransport("", "https://api.the-odds-api.com/v4")

    def test_missing_replay_dataset_fails_loudly(self, tmp_path):
        with pytest.raises(ProviderError, match="replay dataset not found"):
            build_replay_provider(tmp_path)

    def test_model_run_leaves_unknown_data_quality_null(self, priced):
        """Data quality score (§23) ще не реалізований — має бути NULL, а не вигадане число."""
        _session, model_run = priced
        assert model_run.data_quality_score is None
