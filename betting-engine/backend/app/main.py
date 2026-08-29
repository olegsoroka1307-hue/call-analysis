"""FastAPI-шар PoC (ТЗ §52 п.9, підмножина endpoints з §45)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db.base import get_session
from .db.models import Bookmaker, Fixture, MarketPrediction, ModelRun, OddsSnapshot
from .services.movement import get_movement
from .services.pricing import price_fixture, priced_market_to_dict

settings = get_settings()

app = FastAPI(
    title="Betting Decision Engine — PoC",
    version=settings.model_version,
    description=(
        "Proof-of-concept за ТЗ §52. Реалізовано ланцюг: провайдер -> odds snapshots "
        "-> movement -> no-vig -> Poisson -> fair odds -> EV -> рішення."
    ),
)

SessionDep = Annotated[Session, Depends(get_session)]

#: Які рішення взагалі можуть потрапити у список value-кандидатів (ТЗ §45).
#: PASS сюди не входить за визначенням, REVERSE — окремий потік (ТЗ §26).
VALUE_DECISIONS = ("BET", "WATCH")

SYNTHETIC_SOURCES = {"SYNTHETIC_REPLAY", "MIXED"}


def _notice_for_sources(sources: set[str | None]) -> dict | None:
    """Позначка походження саме тих даних, які повернула ця відповідь (ТЗ §1).

    Раніше прапорець рахувався глобально по всій БД: один синтетичний матч
    робив «синтетичною» будь-яку відповідь, а в змішаній live/replay базі
    позначка ставала просто неправильною.
    """
    present = {s for s in sources if s}
    if not present & SYNTHETIC_SOURCES:
        return None
    manifest = Path(settings.replay_data_dir) / "replay_manifest.json"
    warning = "СИНТЕТИЧНІ ДАНІ — не реальний ринок."
    if manifest.exists():
        warning = json.loads(manifest.read_text(encoding="utf-8")).get("warning", warning)
    return {
        "data_source": "MIXED" if len(present) > 1 else next(iter(present)),
        "sources": sorted(present),
        "warning": warning,
    }


def _fixture_label(fixture: Fixture) -> str:
    return f"{fixture.home_team.name} vs {fixture.away_team.name}"


@app.get("/health", summary="Health check (ТЗ §44)")
def health(session: SessionDep) -> dict:
    """Health не має падати разом з БД — усі залежні запити всередині try."""
    database_ok = True
    last_update = None
    snapshots = None
    error: str | None = None
    try:
        session.execute(select(1))
        last_update = session.scalar(select(func.max(OddsSnapshot.source_timestamp)))
        snapshots = session.scalar(select(func.count()).select_from(OddsSnapshot))
    except Exception as exc:  # noqa: BLE001 — деградуємо, а не 500
        database_ok = False
        error = type(exc).__name__

    return {
        "status": "ok" if database_ok else "degraded",
        "database": database_ok,
        "database_error": error,
        "provider": settings.odds_provider,
        "model_version": settings.model_version,
        "last_odds_update": last_update.isoformat() if last_update else None,
        "snapshots": snapshots,
    }


@app.get("/fixtures", summary="Матчі у базі")
def list_fixtures(session: SessionDep) -> dict:
    fixtures = list(session.scalars(select(Fixture).order_by(Fixture.kickoff_at)))
    return {
        "meta": _notice_for_sources({f.data_source for f in fixtures}),
        "count": len(fixtures),
        "fixtures": [
            {
                "id": f.id,
                "provider": f.provider,
                "provider_fixture_id": f.provider_fixture_id,
                "match": _fixture_label(f),
                "league": f.league.name if f.league else None,
                "kickoff_at": f.kickoff_at.isoformat(),
                "status": f.status,
                "data_source": f.data_source,
            }
            for f in fixtures
        ],
    }


@app.get("/fixtures/{fixture_id}/movement", summary="Opening / current / closing (ТЗ §7.2, §28)")
def fixture_movement(
    fixture_id: int,
    session: SessionDep,
    bookmaker: str | None = Query(default=None, description="ключ букмекера, напр. pinnacle"),
) -> dict:
    fixture = session.get(Fixture, fixture_id)
    if fixture is None:
        raise HTTPException(status_code=404, detail="fixture not found")

    combos = session.execute(
        select(OddsSnapshot.bookmaker_id, OddsSnapshot.market_code, OddsSnapshot.selection)
        .where(OddsSnapshot.fixture_id == fixture_id)
        .distinct()
    ).all()
    bookmakers = {b.id: b for b in session.scalars(select(Bookmaker))}

    movements = []
    for bookmaker_id, market_code, selection in combos:
        book = bookmakers[bookmaker_id]
        if bookmaker and book.key != bookmaker:
            continue
        movement = get_movement(
            session, fixture_id, market_code, selection,
            bookmaker_id, book.key, fixture.kickoff_at,
        )
        if movement is not None:
            movements.append(movement.as_dict())

    movements.sort(key=lambda m: (m["market_code"], m["bookmaker"], m["selection"]))
    snapshot_sources = set(
        session.scalars(
            select(OddsSnapshot.data_source)
            .where(OddsSnapshot.fixture_id == fixture_id)
            .distinct()
        )
    )
    return {
        "meta": _notice_for_sources(snapshot_sources | {fixture.data_source}),
        "fixture": {"id": fixture.id, "match": _fixture_label(fixture),
                    "kickoff_at": fixture.kickoff_at.isoformat()},
        "movements": movements,
    }


@app.get("/fixtures/{fixture_id}/markets", summary="Ціноутворення матчу (ТЗ §13-§16)")
def fixture_markets(fixture_id: int, session: SessionDep) -> dict:
    fixture = session.get(Fixture, fixture_id)
    if fixture is None:
        raise HTTPException(status_code=404, detail="fixture not found")

    _run, priced = price_fixture(
        session, fixture,
        lambda_home=settings.poc_lambda_home,
        lambda_away=settings.poc_lambda_away,
        model_version=settings.model_version,
        persist=False,
    )
    return {
        "meta": _notice_for_sources(
            {item.data_source for item in priced} | {fixture.data_source}
        ),
        "fixture": {"id": fixture.id, "match": _fixture_label(fixture),
                    "kickoff_at": fixture.kickoff_at.isoformat()},
        "model": {
            "version": settings.model_version,
            "lambda_home": settings.poc_lambda_home,
            "lambda_away": settings.poc_lambda_away,
            "lambda_total": round(settings.poc_lambda_home + settings.poc_lambda_away, 4),
            "note": "PoC використовує тестові lambda з конфігу; реальні рахуються у Sprint 2 (ТЗ §11).",
        },
        "markets": [priced_market_to_dict(item) for item in priced],
    }


@app.get("/predictions/value", summary="Value candidates (ТЗ §45)")
def value_candidates(
    session: SessionDep,
    min_ev: float = Query(default=0.02, description="мінімальний EV, 0.02 = 2%"),
) -> dict:
    """Лише останній прогін по кожній парі (матч, ринок).

    Без цієї фільтрації endpoint віддавав кандидатів з усіх прогонів одразу:
    одна й та сама ставка дублювалась стільки разів, скільки разів
    запускався run_poc, а зняті лінії зі старих прогонів лишались у видачі.
    """
    if session.scalar(select(func.max(ModelRun.id))) is None:
        return {"meta": None, "count": 0, "candidates": [],
                "hint": "Спочатку запустіть: python -m app.tools.run_poc"}

    latest = (
        select(
            MarketPrediction.fixture_id.label("fixture_id"),
            MarketPrediction.market_code.label("market_code"),
            func.max(MarketPrediction.model_run_id).label("run_id"),
        )
        .group_by(MarketPrediction.fixture_id, MarketPrediction.market_code)
        .subquery()
    )

    rows = list(
        session.scalars(
            select(MarketPrediction)
            .join(
                latest,
                and_(
                    MarketPrediction.fixture_id == latest.c.fixture_id,
                    MarketPrediction.market_code == latest.c.market_code,
                    MarketPrediction.model_run_id == latest.c.run_id,
                ),
            )
            .where(
                MarketPrediction.expected_value >= min_ev,
                MarketPrediction.decision.in_(VALUE_DECISIONS),
            )
            .order_by(MarketPrediction.expected_value.desc())
        )
    )
    bookmakers = {b.id: b.key for b in session.scalars(select(Bookmaker))}
    fixtures = {f.id: f for f in session.scalars(select(Fixture))}
    run_ids = {r.model_run_id for r in rows}
    runs = (
        {r.id: r for r in session.scalars(select(ModelRun).where(ModelRun.id.in_(run_ids)))}
        if run_ids
        else {}
    )

    return {
        "meta": _notice_for_sources(
            {runs[r.model_run_id].data_source for r in rows if r.model_run_id in runs}
        ),
        "min_ev": min_ev,
        "decisions": list(VALUE_DECISIONS),
        "count": len(rows),
        "candidates": [
            {
                "match": _fixture_label(fixtures[r.fixture_id]),
                "kickoff_at": fixtures[r.fixture_id].kickoff_at.isoformat(),
                "bookmaker": bookmakers[r.bookmaker_id],
                "market": r.market_code,
                "selection": r.selection,
                "line": r.line,
                "odds": r.bookmaker_odds,
                "market_probability": round(r.market_probability, 4) if r.market_probability else None,
                "model_probability": round(r.model_probability, 4),
                "fair_odds": round(r.fair_odds, 3) if r.fair_odds else None,
                "expected_value_pct": round(r.expected_value * 100, 2),
                "decision": r.decision,
                "model_run_id": r.model_run_id,
                "data_source": runs[r.model_run_id].data_source if r.model_run_id in runs else None,
            }
            for r in rows
        ],
    }


@app.get("/poc/report", summary="Повний прогін ТЗ §52 для одного матчу")
def poc_report(session: SessionDep, fixture_id: int | None = None) -> dict:
    """Один endpoint, що показує весь ланцюг пунктів 1-8 §52 покроково."""
    fixture = (
        session.get(Fixture, fixture_id)
        if fixture_id
        else session.scalar(select(Fixture).order_by(Fixture.kickoff_at).limit(1))
    )
    if fixture is None:
        raise HTTPException(
            status_code=404,
            detail="У базі немає матчів. Запустіть: python -m app.tools.run_poc",
        )

    movement = fixture_movement(fixture.id, session, bookmaker=None)
    markets = fixture_markets(fixture.id, session)

    totals = [m for m in markets["markets"] if m["market_code"] == "TOTALS"]
    team_totals = [m for m in markets["markets"] if m["market_code"] == "TEAM_TOTALS"]
    return {
        "meta": markets["meta"],
        "generated_at": datetime.now(UTC).isoformat(),
        "spec_section": "§52 — тестове завдання кодеру на 1 день",
        "steps": {
            "1_provider_connected": settings.odds_provider,
            "2_fixtures_loaded": session.scalar(select(func.count()).select_from(Fixture)),
            "3_storage": "PostgreSQL",
            "4_odds_snapshots": session.scalar(
                select(func.count()).select_from(OddsSnapshot)
                .where(OddsSnapshot.fixture_id == fixture.id)
            ),
            "5_movement": movement["movements"],
            "6_7_8_pricing": {"totals": totals, "team_totals": team_totals},
        },
        "fixture": markets["fixture"],
        "model": markets["model"],
    }
