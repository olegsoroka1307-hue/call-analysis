"""Від коефіцієнтів до рішення (ТЗ §52 п.6-8).

Ланцюг: ринкові ціни -> no-vig (§15) -> Poisson score matrix (§12) ->
ймовірність ринку (§13-14) -> fair odds / EV (§16) -> рішення (§25).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Bookmaker, Fixture, MarketPrediction, ModelRun, OddsSnapshot
from ..models_engine.decision import (
    decide,
    disagreement_reason_codes,
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


def _latest_snapshots(session: Session, fixture_id: int) -> list[OddsSnapshot]:
    """Останній снапшот для кожної комбінації (букмекер, ринок, селекція, лінія)."""
    rows = list(
        session.scalars(
            select(OddsSnapshot)
            .where(OddsSnapshot.fixture_id == fixture_id)
            .order_by(OddsSnapshot.source_timestamp.asc(), OddsSnapshot.id.asc())
        )
    )
    latest: dict[tuple, OddsSnapshot] = {}
    for row in rows:
        latest[(row.bookmaker_id, row.market_code, row.selection, row.line)] = row
    return list(latest.values())


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
    snapshots = _latest_snapshots(session, fixture.id)
    bookmakers = {
        b.id: b for b in session.scalars(select(Bookmaker))
    }

    # Групуємо за (букмекер, ринок, scope, лінія), щоб мати обидві сторони для no-vig.
    grouped: dict[tuple, dict[str, OddsSnapshot]] = {}
    for snapshot in snapshots:
        spec = SELECTION_SPEC.get(snapshot.selection)
        if spec is None or snapshot.line is None:
            continue
        _side, scope, _opposite = spec
        grouped.setdefault(
            (snapshot.bookmaker_id, snapshot.market_code, scope, snapshot.line), {}
        )[snapshot.selection] = snapshot

    priced: list[PricedMarket] = []
    used_snapshot_ids: list[int] = []

    for (bookmaker_id, market_code, scope, line), sides in sorted(
        grouped.items(), key=lambda kv: (kv[0][1], kv[0][3], kv[0][0])
    ):
        for selection, snapshot in sorted(sides.items()):
            side, _scope, opposite_selection = SELECTION_SPEC[selection]
            opposite = sides.get(opposite_selection)

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
                market_probability = (
                    two_way.p_over if side is Side.OVER else two_way.p_under
                )
                reason_codes.append(f"MARKET_MARGIN_{two_way.margin_pct:.2f}PCT")

            distribution = outcome_distribution(score_matrix, side, line, scope)
            valuation = evaluate(distribution, snapshot.odds, market_probability)
            disagreement = market_disagreement(
                valuation.model_probability, market_probability
            )
            decision = decide(valuation.expected_value, disagreement)
            reason_codes.extend(disagreement_reason_codes(disagreement))

            if any(f in (0.5, -0.5) for f, p in distribution.items() if p > 0):
                reason_codes.append("ASIAN_SPLIT_STAKE_EV")
            if distribution.get(0.0, 0.0) > 0:
                reason_codes.append("PUSH_POSSIBLE")

            age = (now - snapshot.source_timestamp).total_seconds()
            priced.append(
                PricedMarket(
                    bookmaker=bookmakers[bookmaker_id].key,
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
                    strong_candidate=is_strong_candidate(valuation.expected_value),
                    market_disagreement=disagreement,
                    outcome_distribution={
                        str(f): round(p, 6) for f, p in distribution.items() if p > 0
                    },
                    odds_age_seconds=age,
                    reason_codes=reason_codes,
                )
            )
            used_snapshot_ids.append(snapshot.id)

    if not persist:
        return None, priced

    # ТЗ §51: prediction має бути відтворюваною з model_run.inputs_json.
    model_run = ModelRun(
        fixture_id=fixture.id,
        model_version=model_version,
        lambda_home=lambda_home,
        lambda_away=lambda_away,
        lambda_total=lambda_home + lambda_away,
        market_baseline_used=False,
        data_quality_score=None,
        inputs_json={
            "lambda_home": lambda_home,
            "lambda_away": lambda_away,
            "max_goals": max_goals,
            "data_source": fixture.data_source,
            "snapshot_ids": used_snapshot_ids,
            "priced_at": now.isoformat(),
        },
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
                },
            )
        )
    session.commit()
    return model_run, priced


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
