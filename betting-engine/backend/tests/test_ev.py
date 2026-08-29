"""Fair odds, edge та EV (ТЗ §16), плюс рішення (ТЗ §18, §25)."""

import pytest

from app.models_engine.decision import (
    Decision,
    decide,
    disagreement_reason_codes,
    is_strong_candidate,
    market_disagreement,
)
from app.models_engine.ev import (
    binary_expected_value,
    evaluate,
    expected_value,
    fair_odds,
    fair_probability,
)
from app.models_engine.lines import Side
from app.models_engine.poisson import build_score_matrix, outcome_distribution

HALF_LINE = {1.0: 0.60, 0.5: 0.0, 0.0: 0.0, -0.5: 0.0, -1.0: 0.40}


class TestFairOddsAndEv:
    def test_fair_odds_is_inverse_of_probability_on_half_line(self):
        """ТЗ §16: fair_odds = 1 / model_probability (ринок без push)."""
        assert fair_probability(HALF_LINE) == pytest.approx(0.60)
        assert fair_odds(HALF_LINE) == pytest.approx(1 / 0.60)

    def test_ev_matches_spec_binary_formula_on_half_line(self):
        """ТЗ §16: binary_EV = model_probability * bookmaker_odds - 1."""
        odds = 1.90
        assert expected_value(HALF_LINE, odds) == pytest.approx(
            binary_expected_value(0.60, odds)
        )

    def test_ev_is_zero_at_fair_odds(self):
        assert expected_value(HALF_LINE, fair_odds(HALF_LINE)) == pytest.approx(0.0)

    def test_ev_grows_with_better_price(self):
        assert expected_value(HALF_LINE, 2.00) > expected_value(HALF_LINE, 1.80)

    def test_edge_is_model_minus_market(self):
        """ТЗ §16: edge = model_probability - market_no_vig_probability."""
        valuation = evaluate(HALF_LINE, 1.90, market_probability=0.55)
        assert valuation.edge == pytest.approx(0.60 - 0.55)

    def test_edge_is_none_without_market_price(self):
        assert evaluate(HALF_LINE, 1.90).edge is None

    def test_hopeless_bet_has_no_fair_odds(self):
        assert fair_odds({1.0: 0.0, 0.5: 0.0, 0.0: 0.0, -0.5: 0.0, -1.0: 1.0}) is None

    def test_all_push_is_rejected(self):
        with pytest.raises(ValueError):
            fair_probability({1.0: 0.0, 0.5: 0.0, 0.0: 1.0, -0.5: 0.0, -1.0: 0.0})


class TestAsianEv:
    """ТЗ §16: для asian-ринків використовувати expected payout, а не binary_EV."""

    @pytest.fixture
    def quarter_distribution(self):
        return outcome_distribution(build_score_matrix(1.55, 1.20), Side.OVER, 2.75)

    def test_expected_payout_accounts_for_half_win(self, quarter_distribution):
        win = quarter_distribution[1.0]
        half_win = quarter_distribution[0.5]
        loss = quarter_distribution[-1.0]
        odds = 2.00
        expected = (win + 0.5 * half_win) * (odds - 1) - loss
        assert expected_value(quarter_distribution, odds) == pytest.approx(expected)

    def test_binary_formula_would_overstate_quarter_line_value(self, quarter_distribution):
        """Головна причина правила §16: binary_EV дав би хибний BET-сигнал."""
        odds = 2.00
        naive = binary_expected_value(
            quarter_distribution[1.0] + quarter_distribution[0.5], odds
        )
        correct = expected_value(quarter_distribution, odds)
        assert naive > 0 > correct

    def test_push_adjusted_probability_excludes_push_mass(self):
        """На цілій лінії fair probability рахується без push-маси."""
        distribution = {1.0: 0.45, 0.5: 0.0, 0.0: 0.25, -0.5: 0.0, -1.0: 0.30}
        assert fair_probability(distribution) == pytest.approx(0.45 / 0.75)


class TestDecisionEngine:
    @pytest.mark.parametrize(
        "ev,expected",
        [
            (-0.05, Decision.PASS), (0.0, Decision.PASS), (0.019, Decision.PASS),
            (0.02, Decision.WATCH), (0.039, Decision.WATCH),
            (0.04, Decision.BET), (0.10, Decision.BET),
        ],
    )
    def test_ev_thresholds_follow_spec(self, ev, expected):
        """ТЗ §25: <2% PASS; 2-4% WATCH; >=4% BET."""
        assert decide(ev) is expected

    def test_large_market_disagreement_blocks_bet(self):
        """ТЗ §18: сильне розходження з ринком не є автоматичним value."""
        assert decide(0.10, disagreement=0.02) is Decision.BET
        assert decide(0.10, disagreement=0.15) is Decision.WATCH

    def test_disagreement_is_absolute_difference(self):
        assert market_disagreement(0.70, 0.53) == pytest.approx(0.17)
        assert market_disagreement(0.53, 0.70) == pytest.approx(0.17)
        assert market_disagreement(0.70, None) is None

    def test_deep_review_reason_code_above_seven_points(self):
        assert disagreement_reason_codes(0.08) == ["MARKET_DISAGREEMENT", "REQUIRE_DEEP_REVIEW"]
        assert disagreement_reason_codes(0.05) == ["MARKET_DISAGREEMENT"]
        assert disagreement_reason_codes(0.01) == []

    def test_strong_candidate_threshold(self):
        assert not is_strong_candidate(0.07)
        assert is_strong_candidate(0.071)
