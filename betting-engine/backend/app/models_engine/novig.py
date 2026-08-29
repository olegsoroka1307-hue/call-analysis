"""Ринкова ймовірність і зняття маржі (ТЗ §15)."""

from __future__ import annotations

from dataclasses import dataclass


def raw_implied_probability(odds: float) -> float:
    """ТЗ §15: raw_implied_probability = 1 / odds."""
    if odds <= 1.0:
        raise ValueError(f"decimal odds must be > 1.0, got {odds!r}")
    return 1.0 / odds


@dataclass(frozen=True)
class NoVigTwoWay:
    p_over_raw: float
    p_under_raw: float
    p_over: float
    p_under: float

    @property
    def overround(self) -> float:
        """Маржа букмекера: сума сирих ймовірностей - 1."""
        return self.p_over_raw + self.p_under_raw - 1.0

    @property
    def margin_pct(self) -> float:
        return self.overround * 100.0


def no_vig_two_way(over_odds: float, under_odds: float) -> NoVigTwoWay:
    """Пропорційне зняття маржі для двостороннього ринку (ТЗ §15).

        p_over_no_vig = p_over_raw / (p_over_raw + p_under_raw)
    """
    p_over_raw = raw_implied_probability(over_odds)
    p_under_raw = raw_implied_probability(under_odds)
    total = p_over_raw + p_under_raw
    return NoVigTwoWay(
        p_over_raw=p_over_raw,
        p_under_raw=p_under_raw,
        p_over=p_over_raw / total,
        p_under=p_under_raw / total,
    )
