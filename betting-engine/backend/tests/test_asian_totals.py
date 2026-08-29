"""Asian quarter-lines: split-stake та розрахунок (ТЗ §14, §36.1)."""

import pytest

from app.models_engine.lines import LineType, Side, classify_line, split_line
from app.models_engine.settlement import Settlement, settle_and_pnl, settle_total


class TestLineClassification:
    @pytest.mark.parametrize(
        "line,expected",
        [
            (2.0, LineType.INTEGER), (3.0, LineType.INTEGER),
            (2.5, LineType.HALF), (3.5, LineType.HALF),
            (2.25, LineType.QUARTER), (2.75, LineType.QUARTER), (3.25, LineType.QUARTER),
        ],
    )
    def test_classify(self, line, expected):
        assert classify_line(line) is expected

    def test_rejects_line_off_the_quarter_grid(self):
        with pytest.raises(ValueError):
            classify_line(2.3)

    def test_split_matches_spec_examples(self):
        """ТЗ §14: Over 2.75 = 50% O2.5 + 50% O3.0; Over 3.25 = 50% O3.0 + 50% O3.5."""
        assert split_line(2.75) == [(2.5, 0.5), (3.0, 0.5)]
        assert split_line(3.25) == [(3.0, 0.5), (3.5, 0.5)]

    def test_non_quarter_line_is_single_stake(self):
        assert split_line(2.5) == [(2.5, 1.0)]
        assert split_line(2.0) == [(2.0, 1.0)]


class TestMandatoryAsianSettlement:
    """ТЗ §36.1 — обов'язковий тест. O2.75 @2.00.

        0-2 голи -> LOSS     -1.0u
        3 голи   -> HALF WIN +0.5u
        4+ голів -> WIN      +1.0u
    """

    @pytest.mark.parametrize("goals", [0, 1, 2])
    def test_o275_loses_below_three_goals(self, goals):
        settlement, pnl = settle_and_pnl(Side.OVER, 2.75, goals, odds=2.00)
        assert settlement is Settlement.LOSS
        assert pnl == pytest.approx(-1.0)

    def test_o275_half_wins_on_exactly_three_goals(self):
        settlement, pnl = settle_and_pnl(Side.OVER, 2.75, 3, odds=2.00)
        assert settlement is Settlement.HALF_WIN
        assert pnl == pytest.approx(+0.5)

    @pytest.mark.parametrize("goals", [4, 5, 7])
    def test_o275_wins_from_four_goals(self, goals):
        settlement, pnl = settle_and_pnl(Side.OVER, 2.75, goals, odds=2.00)
        assert settlement is Settlement.WIN
        assert pnl == pytest.approx(+1.0)


class TestOtherAsianLines:
    def test_o225_half_loses_on_exactly_two_goals(self):
        """2.25 = 50% O2.0 (push) + 50% O2.5 (loss) -> HALF_LOSS."""
        settlement, pnl = settle_and_pnl(Side.OVER, 2.25, 2, odds=1.95)
        assert settlement is Settlement.HALF_LOSS
        assert pnl == pytest.approx(-0.5)

    def test_integer_line_pushes(self):
        settlement, pnl = settle_and_pnl(Side.OVER, 2.0, 2, odds=1.95)
        assert settlement is Settlement.PUSH
        assert pnl == 0.0

    def test_under_side_mirrors_over(self):
        assert settle_total(Side.UNDER, 2.75, 2) is Settlement.WIN
        assert settle_total(Side.UNDER, 2.75, 3) is Settlement.HALF_LOSS
        assert settle_total(Side.UNDER, 2.75, 4) is Settlement.LOSS

    def test_half_line_never_pushes(self):
        for goals in range(0, 8):
            assert settle_total(Side.OVER, 2.5, goals) in (Settlement.WIN, Settlement.LOSS)

    def test_half_win_payout_scales_with_odds(self):
        """HALF_WIN платить половину ставки за (odds - 1), а не половину коефіцієнта."""
        _, pnl = settle_and_pnl(Side.OVER, 2.75, 3, odds=3.00, stake_units=2.0)
        assert pnl == pytest.approx(2.0)  # 0.5 * 2u * (3.00 - 1)
