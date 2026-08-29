"""Базова математична модель: Poisson (ТЗ §11-§13).

MVP свідомо використовує Poisson; Dixon-Coles заплановано у 1.1 (ТЗ §11),
тому корекція низьких рахунків тут НЕ застосовується.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import poisson

from .lines import Side, split_line

DEFAULT_MAX_GOALS = 10  # ТЗ §12: мінімум 0-10 голів на команду


@dataclass(frozen=True)
class ScoreMatrix:
    """Спільний розподіл рахунку. matrix[h][a] = P(home=h, away=a)."""

    lambda_home: float
    lambda_away: float
    matrix: np.ndarray

    @property
    def lambda_total(self) -> float:
        return self.lambda_home + self.lambda_away

    @property
    def home_marginal(self) -> np.ndarray:
        return self.matrix.sum(axis=1)

    @property
    def away_marginal(self) -> np.ndarray:
        return self.matrix.sum(axis=0)

    @property
    def total_goals_distribution(self) -> np.ndarray:
        """P(total_goals = t), t = 0..2*max_goals."""
        size = self.matrix.shape[0]
        dist = np.zeros(2 * size - 1)
        for total in range(2 * size - 1):
            lo = max(0, total - size + 1)
            hi = min(total, size - 1)
            dist[total] = sum(self.matrix[h, total - h] for h in range(lo, hi + 1))
        return dist


def build_score_matrix(
    lambda_home: float, lambda_away: float, max_goals: int = DEFAULT_MAX_GOALS
) -> ScoreMatrix:
    """Score matrix з двох незалежних Poisson (ТЗ §12).

    Хвіст за межами max_goals обрізається, тому матриця ренормалізується —
    інакше сума ймовірностей була б < 1 і всі EV системно занижувались би.
    """
    if lambda_home <= 0 or lambda_away <= 0:
        raise ValueError("lambdas must be positive")
    goals = np.arange(0, max_goals + 1)
    home = poisson.pmf(goals, lambda_home)
    away = poisson.pmf(goals, lambda_away)
    matrix = np.outer(home, away)
    return ScoreMatrix(lambda_home, lambda_away, matrix / matrix.sum())


# --------------------------------------------------------------------------
# Ринки (ТЗ §13)
# --------------------------------------------------------------------------

def prob_match_total_over(sm: ScoreMatrix, line: float) -> float:
    """P(Over) для match total, БЕЗ урахування push (сира ймовірність події)."""
    dist = sm.total_goals_distribution
    goals = np.arange(len(dist))
    return float(dist[goals > line].sum())


def prob_match_total_push(sm: ScoreMatrix, line: float) -> float:
    dist = sm.total_goals_distribution
    goals = np.arange(len(dist))
    return float(dist[goals == line].sum())


def prob_team_total_over(sm: ScoreMatrix, line: float, home: bool) -> float:
    marginal = sm.home_marginal if home else sm.away_marginal
    goals = np.arange(len(marginal))
    return float(marginal[goals > line].sum())


def prob_team_total_push(sm: ScoreMatrix, line: float, home: bool) -> float:
    marginal = sm.home_marginal if home else sm.away_marginal
    goals = np.arange(len(marginal))
    return float(marginal[goals == line].sum())


def prob_btts_yes(sm: ScoreMatrix) -> float:
    """P(обидві забивають) = P(home > 0 and away > 0) — ТЗ §13."""
    return float(sm.matrix[1:, 1:].sum())


# --------------------------------------------------------------------------
# Розподіл результатів ставки (потрібен для EV на asian-лініях, ТЗ §14)
# --------------------------------------------------------------------------

def outcome_distribution(
    sm: ScoreMatrix, side: Side, line: float, scope: str = "match"
) -> dict[float, float]:
    """Розподіл результату ставки: {win_fraction: probability}.

    win_fraction: +1.0 WIN, +0.5 HALF_WIN, 0.0 PUSH, -0.5 HALF_LOSS, -1.0 LOSS.
    scope: "match" | "home" | "away".

    Обидві половини quarter-лінії (ТЗ §14) визначаються одним і тим самим
    фактичним рахунком, тобто ідеально корельовані. Тому перебираємо розподіл
    голів і застосовуємо ту саму функцію розрахунку, що й у проді (§36),
    а не перемножуємо половини як незалежні події.
    """
    from .settlement import WIN_FRACTION, settle_total  # локально: уникаємо циклічного імпорту

    if scope == "match":
        goals_distribution = sm.total_goals_distribution
    elif scope == "home":
        goals_distribution = sm.home_marginal
    elif scope == "away":
        goals_distribution = sm.away_marginal
    else:
        raise ValueError(f"unknown scope {scope!r}")

    result: dict[float, float] = {1.0: 0.0, 0.5: 0.0, 0.0: 0.0, -0.5: 0.0, -1.0: 0.0}
    for goals, probability in enumerate(goals_distribution):
        if probability <= 0.0:
            continue
        result[WIN_FRACTION[settle_total(side, line, goals)]] += float(probability)
    return result
