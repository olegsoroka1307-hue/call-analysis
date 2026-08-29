"""Decision engine (ТЗ §25) та anti-bias перевірка (ТЗ §18).

Реалізовано у PoC: EV-пороги §25 і запобіжник market disagreement §18/§25.
НЕ реалізовано (Sprint 3/6/7): data_quality_score §23, confidence A/B/C §24,
reverse-check §26. Ці гілки явно позначені як TODO у `decide`, а не мовчки
пропущені, щоб рішення не виглядало повнішим, ніж воно є.
"""

from __future__ import annotations

from enum import Enum


class Decision(str, Enum):
    BET = "BET"
    WATCH = "WATCH"
    PASS = "PASS"
    REVERSE = "REVERSE"


PASS_BELOW = 0.02              # ТЗ §25: EV < 2%  -> PASS
WATCH_BELOW = 0.04             # ТЗ §25: 2-4%     -> WATCH, >= 4% -> BET
STRONG_FROM = 0.07             # ТЗ §25: > 7%     -> strong candidate

DISAGREEMENT_FLAG = 0.04       # ТЗ §18: розходження з ринком -> прапорець
DISAGREEMENT_DEEP_REVIEW = 0.07  # ТЗ §18/§25: -> deep review, BET заборонений


def market_disagreement(
    model_probability: float, market_probability: float | None
) -> float | None:
    """|модель - ринок| (ТЗ §18). None, якщо ринкової ймовірності немає."""
    if market_probability is None:
        return None
    return abs(model_probability - market_probability)


def decide(
    expected_value: float,
    disagreement: float | None = None,
) -> Decision:
    """Сходинки рішення з ТЗ §25.

    Сильне розходження з ринком НЕ є автоматичним value (ТЗ §18): поки
    injuries / lineup / stale data / provider errors не перевірені, такий
    сигнал може бути лише WATCH, а не BET.
    """
    # TODO Sprint 6: if data_quality < 55 -> PASS (ТЗ §23, §25)
    if expected_value < PASS_BELOW:
        return Decision.PASS
    if expected_value < WATCH_BELOW:
        return Decision.WATCH
    # TODO Sprint 6: if confidence == "C" -> WATCH (ТЗ §24, §25)
    if disagreement is not None and disagreement > DISAGREEMENT_DEEP_REVIEW:
        return Decision.WATCH
    return Decision.BET


def is_strong_candidate(expected_value: float) -> bool:
    """ТЗ §25: strong candidate НЕ означає автоматично більшу ставку."""
    return expected_value > STRONG_FROM


def disagreement_reason_codes(disagreement: float | None) -> list[str]:
    if disagreement is None:
        return []
    if disagreement >= DISAGREEMENT_DEEP_REVIEW:
        return ["MARKET_DISAGREEMENT", "REQUIRE_DEEP_REVIEW"]
    if disagreement >= DISAGREEMENT_FLAG:
        return ["MARKET_DISAGREEMENT"]
    return []
