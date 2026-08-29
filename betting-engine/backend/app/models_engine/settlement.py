"""Розрахунок ставок (ТЗ §36).

Стани: WIN, HALF_WIN, PUSH, HALF_LOSS, LOSS, VOID, PENDING.
Обов'язковий тест ТЗ §36.1 — O2.75 @2.00:
    0-2 голи -> LOSS      -1.0u
    3 голи   -> HALF_WIN  +0.5u
    4+ голів -> WIN       +1.0u
"""

from __future__ import annotations

from enum import Enum

from .lines import Side, split_line


class Settlement(str, Enum):
    WIN = "WIN"
    HALF_WIN = "HALF_WIN"
    PUSH = "PUSH"
    HALF_LOSS = "HALF_LOSS"
    LOSS = "LOSS"
    VOID = "VOID"
    PENDING = "PENDING"


#: Частка ставки, що виграла/програла, для кожного стану.
#: +1.0 = ставка виграла повністю, -0.5 = половина ставки програла, решта повернена.
WIN_FRACTION: dict[Settlement, float] = {
    Settlement.WIN: 1.0,
    Settlement.HALF_WIN: 0.5,
    Settlement.PUSH: 0.0,
    Settlement.HALF_LOSS: -0.5,
    Settlement.LOSS: -1.0,
    Settlement.VOID: 0.0,
}

_FRACTION_TO_SETTLEMENT: dict[float, Settlement] = {
    1.0: Settlement.WIN,
    0.5: Settlement.HALF_WIN,
    0.0: Settlement.PUSH,
    -0.5: Settlement.HALF_LOSS,
    -1.0: Settlement.LOSS,
}


def _settle_half(side: Side, line: float, goals: float) -> float:
    """Одна половина ставки на не-quarter лінії -> +1 / 0 / -1."""
    if goals == line:          # можливо лише на цілій лінії
        return 0.0
    over_won = goals > line
    if side is Side.OVER:
        return 1.0 if over_won else -1.0
    return -1.0 if over_won else 1.0


def settle_total(side: Side, line: float, goals: int) -> Settlement:
    """Розрахувати тотал (match total або team total) за фактичною кількістю голів."""
    fractions = [_settle_half(side, sub_line, goals) for sub_line, _ in split_line(line)]
    net = sum(fractions) / len(fractions)
    return _FRACTION_TO_SETTLEMENT[net]


def pnl_units(settlement: Settlement, odds: float, stake_units: float = 1.0) -> float:
    """P/L в юнітах для розрахованої ставки.

    Виграшна частина платиться за (odds - 1), програшна списується 1:1,
    повернена частина (push) не дає ні прибутку, ні збитку.
    """
    fraction = WIN_FRACTION[settlement]
    if fraction > 0:
        return fraction * stake_units * (odds - 1.0)
    return fraction * stake_units


def settle_and_pnl(
    side: Side, line: float, goals: int, odds: float, stake_units: float = 1.0
) -> tuple[Settlement, float]:
    settlement = settle_total(side, line, goals)
    return settlement, pnl_units(settlement, odds, stake_units)
