"""Від коефіцієнтів до рішення (ТЗ §52 п.6-8).

Ланцюг: ринкові ціни -> no-vig (§15) -> Poisson score matrix (§12) ->
ймовірність ринку (§13-14) -> fair odds / EV (§16) -> рішення (§25).

Дві речі, які тут навмисно зроблені жорстко:

1. **Оцінюється лише актуальна лінія.** Після руху O2.5 -> O2.75 стара 2.5
   більше не існує як ринок. Раніше latest-снапшот брався окремо для кожної
   лінії, тому знята лінія лишалась «доступною» і могла дати сигнал на ставку.
2. **Дані спершу перевіряються, потім оцінюються.** Прострочена ціна,
   відсутня протилежна сторона або снапшот після початку матчу не стають BET
   за жодного EV (ТЗ §1, §22).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Bookmaker, Fixture, MarketPrediction, ModelRun, OddsSnapshot
from ..models_engine.decision import (
    DataGuard,
    decide,
    decision_thresholds,
    disagreement_reason_codes,
    evaluate_data_guard,
    is_strong_candidate,
    market_disagreement,
)
from ..models_engine.ev import evaluate
from ..models_engine.lines import Side
from ..models_engine.novig import no_vig_two_way
from ..models_engine.poisson import DEFAULT_MAX_GOALS, build_score_matrix, outcome_distribution

#: selection у БД -> (сторона ставки, scope для моделі, протилежна сторона)
SELECTION_SPEC: dict[str, tuple[Side, str, str]] = {
    "OVER": (Side.OVER, "match", "UNDER"),
    "UNDER": (Side.UNDER, "match", "OVER"),
    "HOME_OVER": (Side.OVER, "home", "HOME_UNDER"),
    "HOME_UNDER": (Side.UNDER, "home", "HOME_OVER"),
    "AWAY_OVER": (Side.OVER, "away", "AWAY_UNDER"),
    "AWAY_UNDER": (Side.UNDER, "away", "AWAY_OVER"),
}

#: Версія формату inputs_json. Змінюється разом зі складом входів (ТЗ §51).
INPUTS_SCHEMA_VERSION = 2


@dataclass
class PricedMarket:
    bookmaker: str
    market_code: str
    selection: str
    line: float
    bookmaker_odds: float
    market_probability: float | None
    model_probability: float
    fair_odds: float | None
    expected_value: float
    edge: float | None
    decision: str
    strong_candidate: bool
    market_disagreement: float | None
    outcome_distribution: dict[str, float]
    odds_age_seconds: float
    reason_codes: list[str]
    data_source: str
    snapshot_id: int | None = None
    opposite_snapshot_id: int | None = None


@dataclass(frozen=True)
class _Usable:
    """Останній придатний снапшот однієї селекції."""

    snapshot: OddsSnapshot
    after_kickoff: bool


def _latest_usable_by_selection(
    session: Session, fixture: Fixture
) -> dict[tuple[int, str, str], _Usable]:
    """Останній снапшот для кожної (букмекер, ринок, селекція) — БЕЗ лінії у ключі.

    Лінія навмисно не входить у ключ: інакше знята лінія лишається в наборі
    назавжди. Пріоритет — останній снапшот до kickoff; якщо всі снапшоти
    селекції після kickoff, беремо останній, але позначаємо його, щоб
    data-guard перевів рішення в PASS, а не щоб рядок тихо зник.
    """
    rows = list(
        session.scalars(
            select(OddsSnapshot)
            .where(OddsSnapshot.fixture_id == fixture.id)
            .order_by(OddsSnapshot.source_timestamp.asc(), OddsSnapshot.id.asc())
        )
    )

    pre_kickoff: dict[tuple[int, str, str], OddsSnapshot] = {}
    any_snapshot: dict[tuple[int, str, str], OddsSnapshot] = {}
    for row in rows:
        if row.selection not in SELECTION_SPEC or row.line is None:
            continue
        key = (row.bookmaker_id, row.market_code, row.selection)
        any_snapshot[key] = row
        if row.source_timestamp <= fixture.kickoff_at:
            pre_kickoff[key] = row

    usable: dict[tuple[int, str, str], _Usable] = {}
    for key, row in any_snapshot.items():
        if key in pre_kickoff:
            usable[key] = _Usable(snapshot=pre_kickoff[key], after_kickoff=False)
        else:
            usable[key] = _Usable(snapshot=row, after_kickoff=True)
    return usable


def _current_lines(
    usable: dict[tuple[int, str, str], _Usable],
) -> dict[tuple[int, str, str], float]:
    """Актуальна лінія для кожної (букмекер, ринок, scope).

    Актуальною вважається лінія найсвіжішого снапшоту в межах scope: обидві
    сторони тотала рухаються разом, тому саме вона описує ринок «зараз».
    """
    newest: dict[tuple[int, str, str], tuple[datetime, int, float]] = {}
    for (bookmaker_id, market_code, selection), item in usable.items():
        _side, scope, _opposite = SELECTION_SPEC[selection]
        key = (bookmaker_id, market_code, scope)
        stamp = (item.snapshot.source_timestamp, item.snapshot.id, item.snapshot.line)
        if key not in newest or stamp[:2] > newest[key][:2]:
            newest[key] = stamp
    return {key: value[2] for key, value in newest.items()}



def _price_group(
    *,
    bookmaker_key: str,
    market_code: str,
    scope: str,
    line: float,
    sides: dict[str, "_Usable"],
    score_matrix,
    now: datetime,
    thresholds: dict[str, float],
    selection_spec: dict[str, tuple[Side, str, str]],
) -> list[PricedMarket]:
    """Оцінка однієї групи (букмекер, ринок, scope) на одній лінії.

    Виділено окремо навмисно: `price_fixture` і `recompute_from_inputs`
    мусять рахувати ОДНИМ кодом. Паралельна реалізація для перерахунку
    перевіряла б саму себе, а не двигун.
    """
    out: list[PricedMarket] = []
    for selection, item in sorted(sides.items()):
        snapshot = item.snapshot
        side, _scope, opposite_selection = selection_spec[selection]
        opposite_item = sides.get(opposite_selection)
        opposite = opposite_item.snapshot if opposite_item else None

        # ТЗ §1: без протилежної ціни no-vig не рахується, а не вигадується.
        market_probability = None
        reason_codes: list[str] = []
        if opposite is None:
            reason_codes.append("NO_VIG_UNAVAILABLE_MISSING_OPPOSITE_SIDE")
        else:
            two_way = no_vig_two_way(
                snapshot.odds if side is Side.OVER else opposite.odds,
                opposite.odds if side is Side.OVER else snapshot.odds,
            )
            market_probability = two_way.p_over if side is Side.OVER else two_way.p_under
            reason_codes.append(f"MARKET_MARGIN_{two_way.margin_pct:.2f}PCT")

        distribution = outcome_distribution(score_matrix, side, line, scope)
        valuation = evaluate(distribution, snapshot.odds, market_probability)
        disagreement = market_disagreement(valuation.model_probability, market_probability)

        age = (now - snapshot.source_timestamp).total_seconds()
        guard = evaluate_data_guard(
            market_probability=market_probability,
            odds_age_seconds=age,
            snapshot_after_kickoff=item.after_kickoff,
            thresholds=thresholds,
        )
        decision = decide(valuation.expected_value, disagreement, guard, thresholds)
        reason_codes.extend(disagreement_reason_codes(disagreement, thresholds))
        reason_codes.extend(guard.reason_codes)

        if any(f in (0.5, -0.5) for f, p in distribution.items() if p > 0):
            reason_codes.append("ASIAN_SPLIT_STAKE_EV")
        if distribution.get(0.0, 0.0) > 0:
            reason_codes.append("PUSH_POSSIBLE")

        out.append(
            PricedMarket(
                bookmaker=bookmaker_key,
                market_code=market_code,
                selection=selection,
                line=line,
                bookmaker_odds=snapshot.odds,
                market_probability=market_probability,
                model_probability=valuation.model_probability,
                fair_odds=valuation.fair_odds,
                expected_value=valuation.expected_value,
                edge=valuation.edge,
                decision=decision.value,
                strong_candidate=is_strong_candidate(valuation.expected_value, thresholds),
                market_disagreement=disagreement,
                outcome_distribution={
                    str(f): round(p, 6) for f, p in distribution.items() if p > 0
                },
                odds_age_seconds=age,
                reason_codes=reason_codes,
                data_source=snapshot.data_source,
                snapshot_id=snapshot.id,
                opposite_snapshot_id=opposite.id if opposite else None,
            )
        )
    return out


def price_fixture(
    session: Session,
    fixture: Fixture,
    lambda_home: float,
    lambda_away: float,
    model_version: str,
    max_goals: int = DEFAULT_MAX_GOALS,
    persist: bool = True,
    now: datetime | None = None,
) -> tuple[ModelRun | None, list[PricedMarket]]:
    now = now or datetime.now(UTC)
    score_matrix = build_score_matrix(lambda_home, lambda_away, max_goals)
    usable = _latest_usable_by_selection(session, fixture)
    current_line = _current_lines(usable)
    bookmakers = {b.id: b for b in session.scalars(select(Bookmaker))}

    # Групуємо за (букмекер, ринок, scope), лишаючи тільки сторони на актуальній лінії.
    grouped: dict[tuple, dict[str, _Usable]] = {}
    for (bookmaker_id, market_code, selection), item in usable.items():
        _side, scope, _opposite = SELECTION_SPEC[selection]
        scope_key = (bookmaker_id, market_code, scope)
        if item.snapshot.line != current_line.get(scope_key):
            continue  # знята лінія — це вже не ринок
        grouped.setdefault(scope_key, {})[selection] = item

    priced: list[PricedMarket] = []
    used_snapshot_ids: list[int] = []
    thresholds = decision_thresholds()

    for (bookmaker_id, market_code, scope), sides in sorted(
        grouped.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])
    ):
        line = current_line[(bookmaker_id, market_code, scope)]
        group = _price_group(
            bookmaker_key=bookmakers[bookmaker_id].key,
            market_code=market_code,
            scope=scope,
            line=line,
            sides=sides,
            score_matrix=score_matrix,
            now=now,
            thresholds=thresholds,
            selection_spec=SELECTION_SPEC,
        )
        priced.extend(group)
        used_snapshot_ids.extend(item.snapshot_id for item in group if item.snapshot_id)

    if not persist:
        return None, priced

    inputs = build_inputs_json(
        lambda_home=lambda_home,
        lambda_away=lambda_away,
        max_goals=max_goals,
        model_version=model_version,
        snapshot_ids=used_snapshot_ids,
        prediction_cutoff=now,
        data_source=fixture.data_source,
    )

    model_run = ModelRun(
        fixture_id=fixture.id,
        model_version=model_version,
        lambda_home=lambda_home,
        lambda_away=lambda_away,
        lambda_total=lambda_home + lambda_away,
        market_baseline_used=False,
        data_quality_score=None,
        data_source=_run_data_source(priced, fixture),
        inputs_json=inputs,
    )
    session.add(model_run)
    session.flush()

    bookmaker_by_key = {b.key: b.id for b in bookmakers.values()}
    for item in priced:
        session.add(
            MarketPrediction(
                model_run_id=model_run.id,
                fixture_id=fixture.id,
                bookmaker_id=bookmaker_by_key[item.bookmaker],
                market_code=item.market_code,
                selection=item.selection,
                line=item.line,
                bookmaker_odds=item.bookmaker_odds,
                market_probability=item.market_probability,
                model_probability=item.model_probability,
                fair_odds=item.fair_odds,
                expected_value=item.expected_value,
                decision=item.decision,
                reason_codes_json={
                    "codes": item.reason_codes,
                    "outcome_distribution": item.outcome_distribution,
                    "snapshot_id": item.snapshot_id,
                    "opposite_snapshot_id": item.opposite_snapshot_id,
                    "odds_age_seconds": item.odds_age_seconds,
                    "edge": item.edge,
                    "market_disagreement": item.market_disagreement,
                    "strong_candidate": item.strong_candidate,
                    "data_source": item.data_source,
                },
            )
        )
    session.commit()
    return model_run, priced


def _run_data_source(priced: list[PricedMarket], fixture: Fixture) -> str:
    """Походження прогону = походження даних, на яких він реально порахований."""
    sources = {item.data_source for item in priced}
    if not sources:
        return fixture.data_source
    if len(sources) == 1:
        return sources.pop()
    return "MIXED"


def build_inputs_json(
    *,
    lambda_home: float,
    lambda_away: float,
    max_goals: int,
    model_version: str,
    snapshot_ids: list[int],
    prediction_cutoff: datetime,
    data_source: str,
) -> dict:
    """Повний набір входів прогону (ТЗ §51).

    Достатній, щоб перерахувати кожну prediction з нуля: моделі, пороги
    рішень, мапа ринків, використані снапшоти і момент зрізу. `inputs_hash`
    фіксує весь набір одним значенням.
    """
    payload = {
        "schema_version": INPUTS_SCHEMA_VERSION,
        "model_version": model_version,
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "max_goals": max_goals,
        "data_source": data_source,
        "snapshot_ids": sorted(snapshot_ids),
        "prediction_cutoff": prediction_cutoff.isoformat(),
        "priced_at": prediction_cutoff.isoformat(),
        "decision_thresholds": decision_thresholds(),
        "market_mapping": {
            selection: {"side": side.value, "scope": scope, "opposite": opposite}
            for selection, (side, scope, opposite) in SELECTION_SPEC.items()
        },
        "feature_flags": {
            "market_baseline_used": False,
            "dixon_coles": False,          # ТЗ §11, Sprint 10
            "data_quality_score": False,   # ТЗ §23, Sprint 6
            "confidence_grade": False,     # ТЗ §24, Sprint 6
            "reverse_engine": False,       # ТЗ §26, Sprint 7
        },
    }
    payload["inputs_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def priced_market_to_dict(item: PricedMarket) -> dict:
    payload = asdict(item)
    payload["model_probability"] = round(item.model_probability, 6)
    payload["expected_value"] = round(item.expected_value, 6)
    payload["expected_value_pct"] = round(item.expected_value * 100, 2)
    if item.market_probability is not None:
        payload["market_probability"] = round(item.market_probability, 6)
    if item.edge is not None:
        payload["edge"] = round(item.edge, 6)
    if item.fair_odds is not None:
        payload["fair_odds"] = round(item.fair_odds, 4)
    return payload


def recompute_from_inputs(session: Session, inputs: dict) -> list[PricedMarket]:
    """Повний перерахунок prediction ЛИШЕ з `model_runs.inputs_json` (ТЗ §51).

    Функція навмисно не приймає ні fixture, ні model_run, ні збережені
    predictions: єдиний вхід — записаний JSON і снапшоти, на які він
    посилається. Снапшоти append-only, тому вони не могли змінитися
    заднім числом.

    Пороги, мапа ринків і момент зрізу беруться з `inputs`, а не з поточних
    констант модуля. Інакше зміна порогу перепризначала б рішення старих
    прогонів, і «відтворюваність» означала б лише «числа збігаються, поки
    конфіг не чіпали».
    """
    snapshot_ids = inputs["snapshot_ids"]
    if not snapshot_ids:
        return []

    thresholds = inputs["decision_thresholds"]
    now = datetime.fromisoformat(inputs["prediction_cutoff"])
    selection_spec = {
        selection: (Side(spec["side"]), spec["scope"], spec["opposite"])
        for selection, spec in inputs["market_mapping"].items()
    }
    score_matrix = build_score_matrix(
        inputs["lambda_home"], inputs["lambda_away"], inputs["max_goals"]
    )

    snapshots = list(
        session.scalars(select(OddsSnapshot).where(OddsSnapshot.id.in_(snapshot_ids)))
    )
    bookmakers = {b.id: b.key for b in session.scalars(select(Bookmaker))}

    grouped: dict[tuple, dict[str, _Usable]] = {}
    for snapshot in snapshots:
        spec = selection_spec.get(snapshot.selection)
        if spec is None or snapshot.line is None:
            continue
        _side, scope, _opposite = spec
        after_kickoff = snapshot.source_timestamp > snapshot.fixture.kickoff_at
        grouped.setdefault(
            (snapshot.bookmaker_id, snapshot.market_code, scope, snapshot.line), {}
        )[snapshot.selection] = _Usable(snapshot=snapshot, after_kickoff=after_kickoff)

    priced: list[PricedMarket] = []
    for (bookmaker_id, market_code, scope, line), sides in sorted(
        grouped.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])
    ):
        priced.extend(
            _price_group(
                bookmaker_key=bookmakers[bookmaker_id],
                market_code=market_code,
                scope=scope,
                line=line,
                sides=sides,
                score_matrix=score_matrix,
                now=now,
                thresholds=thresholds,
                selection_spec=selection_spec,
            )
        )
    return priced
