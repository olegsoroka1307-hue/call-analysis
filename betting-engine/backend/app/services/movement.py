"""Opening / current / closing та рух лінії (ТЗ §7.2, §28, §52 п.5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import OddsSnapshot

#: ТЗ §28: падіння коефіцієнта > 5% -> STEAM, зростання > 5% -> DRIFT.
STEAM_THRESHOLD = 0.05


@dataclass(frozen=True)
class Movement:
    market_code: str
    selection: str
    bookmaker_key: str
    opening_odds: float
    opening_line: float | None
    opening_at: datetime
    current_odds: float
    current_line: float | None
    current_at: datetime
    closing_odds: float | None
    closing_line: float | None
    snapshot_count: int

    @property
    def price_move(self) -> float:
        """Відносна зміна ціни: current / opening - 1."""
        return self.current_odds / self.opening_odds - 1.0

    @property
    def line_move(self) -> float | None:
        if self.opening_line is None or self.current_line is None:
            return None
        return self.current_line - self.opening_line

    @property
    def signal(self) -> str:
        """ТЗ §28: рух ЛІНІЇ і рух ЦІНИ — різні події, не змішуємо."""
        line_move = self.line_move
        if line_move:
            return "TOTAL_LINE_UP" if line_move > 0 else "TOTAL_LINE_DOWN"
        if self.price_move <= -STEAM_THRESHOLD:
            return "STEAM"
        if self.price_move >= STEAM_THRESHOLD:
            return "DRIFT"
        return "FLAT"

    def as_dict(self) -> dict:
        return {
            "market_code": self.market_code,
            "selection": self.selection,
            "bookmaker": self.bookmaker_key,
            "opening": {
                "odds": self.opening_odds,
                "line": self.opening_line,
                "at": self.opening_at.isoformat(),
            },
            "current": {
                "odds": self.current_odds,
                "line": self.current_line,
                "at": self.current_at.isoformat(),
            },
            "closing": {"odds": self.closing_odds, "line": self.closing_line},
            "snapshot_count": self.snapshot_count,
            "price_move_pct": round(self.price_move * 100, 2),
            "line_move": self.line_move,
            "signal": self.signal,
        }


def _history(
    session: Session, fixture_id: int, market_code: str, selection: str, bookmaker_id: int | None
) -> list[OddsSnapshot]:
    stmt = (
        select(OddsSnapshot)
        .where(
            OddsSnapshot.fixture_id == fixture_id,
            OddsSnapshot.market_code == market_code,
            OddsSnapshot.selection == selection,
        )
        .order_by(OddsSnapshot.source_timestamp.asc(), OddsSnapshot.id.asc())
    )
    if bookmaker_id is not None:
        stmt = stmt.where(OddsSnapshot.bookmaker_id == bookmaker_id)
    return list(session.scalars(stmt))


def get_movement(
    session: Session,
    fixture_id: int,
    market_code: str,
    selection: str,
    bookmaker_id: int,
    bookmaker_key: str,
    kickoff_at: datetime | None = None,
) -> Movement | None:
    """ТЗ §7.2: opening = перший валідний snapshot, current = останній,
    closing = останній snapshot СТРОГО до kickoff."""
    history = _history(session, fixture_id, market_code, selection, bookmaker_id)
    if not history:
        return None

    opening, current = history[0], history[-1]
    closing = None
    if kickoff_at is not None:
        before_kickoff = [s for s in history if s.source_timestamp < kickoff_at]
        closing = before_kickoff[-1] if before_kickoff else None

    return Movement(
        market_code=market_code,
        selection=selection,
        bookmaker_key=bookmaker_key,
        opening_odds=opening.odds,
        opening_line=opening.line,
        opening_at=opening.source_timestamp,
        current_odds=current.odds,
        current_line=current.line,
        current_at=current.source_timestamp,
        closing_odds=closing.odds if closing else None,
        closing_line=closing.line if closing else None,
        snapshot_count=len(history),
    )


def latest_snapshot(
    session: Session, fixture_id: int, market_code: str, selection: str,
    bookmaker_id: int, line: float | None = None,
) -> OddsSnapshot | None:
    stmt = (
        select(OddsSnapshot)
        .where(
            OddsSnapshot.fixture_id == fixture_id,
            OddsSnapshot.market_code == market_code,
            OddsSnapshot.selection == selection,
            OddsSnapshot.bookmaker_id == bookmaker_id,
        )
        .order_by(OddsSnapshot.source_timestamp.desc(), OddsSnapshot.id.desc())
        .limit(1)
    )
    if line is not None:
        stmt = stmt.where(OddsSnapshot.line == line)
    return session.scalar(stmt)
