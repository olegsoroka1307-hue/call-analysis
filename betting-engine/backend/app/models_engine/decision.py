"""Decision engine (ТЗ §25), anti-bias перевірка (ТЗ §18) та захист даних (ТЗ §1, §22).

Реалізовано у PoC: EV-пороги §25, запобіжник market disagreement §18/§25 і
жорсткий data-guard, який не дає простроченим або неповним даним стати BET.

НЕ реалізовано (Sprint 3/6/7): data_quality_score §23, confidence A/B/C §24,
reverse-check §26. Ці гілки явно позначені як TODO у `decide`, а не мовчки
пропущені, щоб рішення не виглядало повнішим, ніж воно є.
"""

from __future__ import annotations

from dataclasses import dataclass
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

#: Ціна старіша за це — ще торгується, але BET уже не дозволений (ТЗ §22).
STALE_WATCH_SECONDS = 300.0
#: Ціна старіша за це — рішення неможливе взагалі.
STALE_PASS_SECONDS = 900.0
#: Допуск на розсинхрон годинників провайдера. Більший «майбутній» вік — помилка.
CLOCK_SKEW_TOLERANCE_SECONDS = 5.0


@dataclass(frozen=True)
class DataGuard:
    """Результат перевірки придатності даних до рішення (ТЗ §1, §22).

    `blocks_bet` означає «BET заборонений», `forced` — рішення, яке
    підставляється замість розрахованого.
    """

    forced: Decision | None
    reason_codes: tuple[str, ...]

    @property
    def blocks_bet(self) -> bool:
        return self.forced is not None


def evaluate_data_guard(
    *,
    market_probability: float | None,
    odds_age_seconds: float | None,
    snapshot_after_kickoff: bool = False,
) -> DataGuard:
    """Чи можна взагалі приймати рішення за цими даними.

    ТЗ §1: відсутні дані не підмінюються здогадками. Порожня протилежна
    сторона означає, що no-vig не порахований — на такому ринку BET
    неможливий за визначенням, а не «майже можливий».
    """
    codes: list[str] = []
    forced: Decision | None = None

    if snapshot_after_kickoff:
        codes.append("SNAPSHOT_AFTER_KICKOFF")
        forced = Decision.PASS

    if odds_age_seconds is None:
        codes.append("ODDS_AGE_UNKNOWN")
        forced = Decision.PASS
    elif odds_age_seconds < -CLOCK_SKEW_TOLERANCE_SECONDS:
        # Ціна «з майбутнього» — це помилка провайдера, а не свіжі дані.
        codes.append("PROVIDER_ERROR_FUTURE_TIMESTAMP")
        forced = Decision.PASS
    elif odds_age_seconds > STALE_PASS_SECONDS:
        codes.append("STALE_ODDS_PASS")
        forced = Decision.PASS
    elif odds_age_seconds > STALE_WATCH_SECONDS:
        codes.append("STALE_ODDS_WATCH")
        if forced is None:
            forced = Decision.WATCH

    if market_probability is None:
        codes.append("DATA_UNAVAILABLE")
        forced = Decision.PASS

    return DataGuard(forced=forced, reason_codes=tuple(codes))


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
    guard: DataGuard | None = None,
) -> Decision:
    """Сходинки рішення з ТЗ §25.

    Сильне розходження з ринком НЕ є автоматичним value (ТЗ §18): поки
    injuries / lineup / stale data / provider errors не перевірені, такий
    сигнал може бути лише WATCH, а не BET.

    `guard` (ТЗ §1, §22) має пріоритет над EV: прострочена ціна або
    відсутня протилежна сторона не стають BET за жодного EV.
    """
    # TODO Sprint 6: if data_quality < 55 -> PASS (ТЗ §23, §25)
    if guard is not None and guard.forced is Decision.PASS:
        return Decision.PASS

    if expected_value < PASS_BELOW:
        return Decision.PASS
    if expected_value < WATCH_BELOW:
        return Decision.WATCH
    # TODO Sprint 6: if confidence == "C" -> WATCH (ТЗ §24, §25)
    if disagreement is not None and disagreement >= DISAGREEMENT_DEEP_REVIEW:
        return Decision.WATCH
    if guard is not None and guard.forced is Decision.WATCH:
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


#: Пороги, що потрапляють у model_runs.inputs_json (ТЗ §51) — рішення має
#: відтворюватися разом з конфігурацією, за якої воно було прийняте.
def decision_thresholds() -> dict[str, float]:
    return {
        "pass_below": PASS_BELOW,
        "watch_below": WATCH_BELOW,
        "strong_from": STRONG_FROM,
        "disagreement_flag": DISAGREEMENT_FLAG,
        "disagreement_deep_review": DISAGREEMENT_DEEP_REVIEW,
        "stale_watch_seconds": STALE_WATCH_SECONDS,
        "stale_pass_seconds": STALE_PASS_SECONDS,
        "clock_skew_tolerance_seconds": CLOCK_SKEW_TOLERANCE_SECONDS,
    }
