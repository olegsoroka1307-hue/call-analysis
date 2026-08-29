"""Fair odds, edge та EV (ТЗ §16).

Для двосторонніх ринків без push працюють формули ТЗ напряму:
    fair_odds = 1 / model_probability
    binary_EV = model_probability * bookmaker_odds - 1

Для asian-ліній ТЗ §16 вимагає expected payout замість binary_EV. Обидва
випадки покриває один розрахунок через розподіл результату ставки:

    A = очікувана виграшна частка ставки   (sum по результатах з fraction > 0)
    B = очікувана програшна частка ставки  (sum по результатах з fraction < 0)

    EV(odds)  = A * (odds - 1) - B
    fair_odds = 1 + B / A          (odds, за яких EV = 0)
    model_probability = A / (A + B)   (fair probability з поправкою на push)

На half-лінії A = P(win), B = P(loss) = 1 - A, і формули збігаються з ТЗ §16.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Valuation:
    model_probability: float
    fair_odds: float | None
    bookmaker_odds: float
    expected_value: float
    market_probability: float | None = None
    edge: float | None = None

    @property
    def expected_value_pct(self) -> float:
        return self.expected_value * 100.0


def _split_fractions(outcome_distribution: dict[float, float]) -> tuple[float, float]:
    win_side = sum(p * f for f, p in outcome_distribution.items() if f > 0)
    loss_side = sum(p * -f for f, p in outcome_distribution.items() if f < 0)
    return win_side, loss_side


def fair_probability(outcome_distribution: dict[float, float]) -> float:
    """Push-adjusted fair probability: A / (A + B)."""
    win_side, loss_side = _split_fractions(outcome_distribution)
    denominator = win_side + loss_side
    if denominator <= 0:
        raise ValueError("degenerate outcome distribution: everything pushes")
    return win_side / denominator


def fair_odds(outcome_distribution: dict[float, float]) -> float | None:
    """Коефіцієнт, за якого EV дорівнює нулю. None, якщо виграш неможливий."""
    win_side, loss_side = _split_fractions(outcome_distribution)
    if win_side <= 0:
        return None
    return 1.0 + loss_side / win_side


def expected_value(outcome_distribution: dict[float, float], bookmaker_odds: float) -> float:
    """EV на 1 юніт ставки. Для asian-ліній це expected payout (ТЗ §16)."""
    win_side, loss_side = _split_fractions(outcome_distribution)
    return win_side * (bookmaker_odds - 1.0) - loss_side


def binary_expected_value(model_probability: float, bookmaker_odds: float) -> float:
    """Пряма формула ТЗ §16 для ринків без push."""
    return model_probability * bookmaker_odds - 1.0


def evaluate(
    outcome_distribution: dict[float, float],
    bookmaker_odds: float,
    market_probability: float | None = None,
) -> Valuation:
    model_probability = fair_probability(outcome_distribution)
    edge = None if market_probability is None else model_probability - market_probability
    return Valuation(
        model_probability=model_probability,
        fair_odds=fair_odds(outcome_distribution),
        bookmaker_odds=bookmaker_odds,
        expected_value=expected_value(outcome_distribution, bookmaker_odds),
        market_probability=market_probability,
        edge=edge,
    )
