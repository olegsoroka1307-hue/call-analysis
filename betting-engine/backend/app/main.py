"""FastAPI-шар PoC (ТЗ §52 п.9, підмножина endpoints з §45)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import func, select
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


def _data_source_notice(session: Session) -> dict | None:
    """ТЗ §1: походження даних має бути видимим, а не мовчазним."""
    sources = set(session.scalars(select(Fixture.data_source).distinct()))
    if "SYNTHETIC_REPLAY" not in sources:
        return None
    manifest = Path(settings.replay_data_dir) / "replay_manifest.json"
    warning = "СИНТЕТИЧНІ ДАНІ — не реальний ринок."
    if manifest.exists():
        warning = json.loads(manifest.read_text(encoding="utf-8")).get("warning", warning)
    return {"data_source": "SYNTHETIC_REPLAY", "warning": warning}


def _fixture_label(fixture: Fixture) -> str:
    return f"{fixture.home_team.name} vs {fixture.away_team.name}"


@app.get("/health", summary="Health check (ТЗ §44)")
def health(session: SessionDep) -> dict:
    try:
        session.execute(select(1))
        database_ok = True
    except Exception:  # noqa: BLE001 — health не має падати разом з БД
        database_ok = False
    last_update = session.scalar(select(func.max(OddsSnapshot.source_timestamp)))
    return {
        "status": "ok" if database_ok else "degraded",
        "database": database_ok,
        "provider": settings.odds_provider,
        "model_version": settings.model_version,
        "last_odds_update": last_update.isoformat() if last_update else None,
        "snapshots": session.scalar(select(func.count()).select_from(OddsSnapshot)),
    }


@app.get("/fixtures", summary="Матчі у базі")
def list_fixtures(session: SessionDep) -> dict:
    fixtures = list(session.scalars(select(Fixture).order_by(Fixture.kickoff_at)))
    return {
        "meta": _data_source_notice(session),
        "count": len(fixtures),
        "fixtures": [
            {
                "id": f.id,
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
    return {
        "meta": _data_source_notice(session),
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
        "meta": _data_source_notice(session),
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
    latest_run = session.scalar(select(func.max(ModelRun.id)))
    if latest_run is None:
        return {"meta": _data_source_notice(session), "count": 0, "candidates": [],
                "hint": "Спочатку запустіть: python -m app.tools.run_poc"}

    rows = list(
        session.scalars(
            select(MarketPrediction)
            .where(MarketPrediction.expected_value >= min_ev)
            .order_by(MarketPrediction.expected_value.desc())
        )
    )
    bookmakers = {b.id: b.key for b in session.scalars(select(Bookmaker))}
    fixtures = {f.id: f for f in session.scalars(select(Fixture))}
    return {
        "meta": _data_source_notice(session),
        "min_ev": min_ev,
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
        "meta": _data_source_notice(session),
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
