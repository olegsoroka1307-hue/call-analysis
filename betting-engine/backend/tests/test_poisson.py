"""Poisson score matrix і ринки (ТЗ §11-§13)."""

import numpy as np
import pytest
from scipy.stats import poisson

from app.models_engine.lines import Side
from app.models_engine.poisson import (
    build_score_matrix,
    outcome_distribution,
    prob_btts_yes,
    prob_match_total_over,
    prob_match_total_push,
    prob_team_total_over,
)

LAMBDA_HOME, LAMBDA_AWAY = 1.55, 1.20


@pytest.fixture
def score_matrix():
    return build_score_matrix(LAMBDA_HOME, LAMBDA_AWAY)


class TestScoreMatrix:
    def test_matrix_is_a_probability_distribution(self, score_matrix):
        assert score_matrix.matrix.sum() == pytest.approx(1.0)
        assert (score_matrix.matrix >= 0).all()

    def test_covers_at_least_zero_to_ten_goals(self, score_matrix):
        """ТЗ §12: мінімум 0-10 голів на команду."""
        assert score_matrix.matrix.shape[0] >= 11

    def test_lambda_total_is_sum_of_lambdas(self, score_matrix):
        assert score_matrix.lambda_total == pytest.approx(LAMBDA_HOME + LAMBDA_AWAY)

    def test_marginals_match_poisson(self, score_matrix):
        """Після ренормалізації маргіналі мають лишатися Poisson з тим самим lambda."""
        expected = poisson.pmf(np.arange(11), LAMBDA_HOME)
        assert score_matrix.home_marginal == pytest.approx(expected, abs=1e-6)

    def test_total_goals_distribution_sums_to_one(self, score_matrix):
        assert score_matrix.total_goals_distribution.sum() == pytest.approx(1.0)

    @pytest.mark.parametrize("lh,la", [(0, 1.2), (-1, 1.2), (1.5, 0)])
    def test_rejects_non_positive_lambdas(self, lh, la):
        with pytest.raises(ValueError):
            build_score_matrix(lh, la)


class TestMarkets:
    def test_over_25_equals_three_or_more_goals(self, score_matrix):
        """ТЗ §13: P(Over 2.5) = P(total_goals >= 3)."""
        distribution = score_matrix.total_goals_distribution
        assert prob_match_total_over(score_matrix, 2.5) == pytest.approx(
            distribution[3:].sum()
        )

    def test_team_total_over_25_equals_three_or_more_home_goals(self, score_matrix):
        """ТЗ §13: P(Home TT Over 2.5) = P(home_goals >= 3)."""
        assert prob_team_total_over(score_matrix, 2.5, home=True) == pytest.approx(
            score_matrix.home_marginal[3:].sum()
        )

    def test_btts_equals_both_teams_scoring(self, score_matrix):
        """ТЗ §13: P(BTTS Yes) = P(home > 0 and away > 0)."""
        p_home_zero = float(score_matrix.home_marginal[0])
        p_away_zero = float(score_matrix.away_marginal[0])
        expected = (1 - p_home_zero) * (1 - p_away_zero)  # Poisson-и незалежні
        assert prob_btts_yes(score_matrix) == pytest.approx(expected, abs=1e-6)

    def test_half_line_has_no_push(self, score_matrix):
        assert prob_match_total_push(score_matrix, 2.5) == pytest.approx(0.0)

    def test_integer_line_has_push_mass(self, score_matrix):
        assert prob_match_total_push(score_matrix, 2.0) > 0.0

    def test_over_probability_decreases_as_line_rises(self, score_matrix):
        probabilities = [prob_match_total_over(score_matrix, l) for l in (1.5, 2.5, 3.5, 4.5)]
        assert probabilities == sorted(probabilities, reverse=True)


class TestOutcomeDistribution:
    @pytest.mark.parametrize("line", [2.0, 2.25, 2.5, 2.75, 3.0])
    def test_always_sums_to_one(self, score_matrix, line):
        distribution = outcome_distribution(score_matrix, Side.OVER, line)
        assert sum(distribution.values()) == pytest.approx(1.0)

    def test_quarter_line_produces_half_outcomes(self, score_matrix):
        distribution = outcome_distribution(score_matrix, Side.OVER, 2.75)
        assert distribution[0.5] > 0      # half win можливий
        assert distribution[0.0] == 0.0   # push на quarter-лінії неможливий

    def test_half_line_is_binary(self, score_matrix):
        distribution = outcome_distribution(score_matrix, Side.OVER, 2.5)
        assert distribution[0.5] == 0.0
        assert distribution[-0.5] == 0.0
        assert distribution[0.0] == 0.0

    def test_over_and_under_are_complementary(self, score_matrix):
        over = outcome_distribution(score_matrix, Side.OVER, 2.75)
        under = outcome_distribution(score_matrix, Side.UNDER, 2.75)
        assert over[1.0] == pytest.approx(under[-1.0])
        assert over[0.5] == pytest.approx(under[-0.5])

    def test_unknown_scope_is_rejected(self, score_matrix):
        with pytest.raises(ValueError):
            outcome_distribution(score_matrix, Side.OVER, 2.5, scope="goalkeeper")
