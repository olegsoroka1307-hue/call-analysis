"""Зняття маржі (ТЗ §15)."""

import pytest

from app.models_engine.novig import no_vig_two_way, raw_implied_probability


def test_raw_implied_probability():
    assert raw_implied_probability(2.0) == pytest.approx(0.5)
    assert raw_implied_probability(1.25) == pytest.approx(0.8)


@pytest.mark.parametrize("odds", [1.0, 0.5, -2.0])
def test_rejects_impossible_odds(odds):
    with pytest.raises(ValueError):
        raw_implied_probability(odds)


def test_no_vig_probabilities_sum_to_one():
    result = no_vig_two_way(1.95, 1.87)
    assert result.p_over + result.p_under == pytest.approx(1.0)


def test_no_vig_follows_spec_formula():
    """ТЗ §15: p_over_no_vig = p_over_raw / (p_over_raw + p_under_raw)."""
    over_odds, under_odds = 1.95, 1.87
    p_over_raw, p_under_raw = 1 / over_odds, 1 / under_odds
    expected = p_over_raw / (p_over_raw + p_under_raw)
    assert no_vig_two_way(over_odds, under_odds).p_over == pytest.approx(expected)


def test_overround_is_positive_for_real_market():
    result = no_vig_two_way(1.95, 1.87)
    assert result.overround > 0
    assert result.margin_pct == pytest.approx(result.overround * 100)


def test_fair_market_has_zero_margin():
    result = no_vig_two_way(2.0, 2.0)
    assert result.overround == pytest.approx(0.0)
    assert result.p_over == pytest.approx(0.5)


def test_no_vig_removes_margin_symmetrically():
    """Зняття маржі не має зсувати ринок у бік однієї зі сторін."""
    result = no_vig_two_way(2.10, 1.80)
    assert result.p_over < result.p_over_raw
    assert result.p_under < result.p_under_raw
