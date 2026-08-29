"""Схема БД — підмножина ТЗ §7.1, потрібна для PoC (ТЗ §52).

Свідомо НЕ реалізовано у PoC: team_match_stats, injuries, lineups, alerts, bets.
Вони належать до Sprint 2/6/7/8 і не потрібні для пунктів 1-10 §52.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class League(Base):
    __tablename__ = "leagues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Провайдер, у чиєму просторі імен унікальний provider_id (ТЗ §6).
    provider: Mapped[str] = mapped_column(String(32), default="the_odds_api")
    provider_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(64))
    season: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("provider", "provider_id", name="uq_leagues_provider_provider_id"),
    )


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Імена команд у різних провайдерів не збігаються — глобальна
    #: унікальність по name зліпила б дві різні команди в одну (ТЗ §6).
    provider: Mapped[str] = mapped_column(String(32), default="the_odds_api")
    name: Mapped[str] = mapped_column(String(128))
    league_id: Mapped[int | None] = mapped_column(ForeignKey("leagues.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("provider", "name", name="uq_teams_provider_name"),
    )


class Fixture(Base):
    __tablename__ = "fixtures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="the_odds_api")
    provider_fixture_id: Mapped[str] = mapped_column(String(128))
    league_id: Mapped[int | None] = mapped_column(ForeignKey("leagues.id"))
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    kickoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="NS")
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)
    #: Походження даних. SYNTHETIC_REPLAY має бути видимим до самого API,
    #: щоб офлайн-датасет неможливо було сплутати з реальним ринком (ТЗ §1).
    data_source: Mapped[str] = mapped_column(String(32), default="LIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    home_team: Mapped[Team] = relationship(foreign_keys=[home_team_id], lazy="joined")
    away_team: Mapped[Team] = relationship(foreign_keys=[away_team_id], lazy="joined")
    league: Mapped[League | None] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_fixture_id", name="uq_fixtures_provider_fixture_id"
        ),
        Index("ix_fixtures_kickoff_at", "kickoff_at"),
    )


class Bookmaker(Base):
    __tablename__ = "bookmakers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    is_sharp: Mapped[bool] = mapped_column(Boolean, default=False)
    is_preferred: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (UniqueConstraint("key", name="uq_bookmakers_key"),)


class OddsSnapshot(Base):
    """Append-only історія коефіцієнтів (ТЗ §7.2).

    Кожна зміна ціни = окремий INSERT. UPDATE/DELETE заборонені тригером БД
    (див. міграцію), а не лише домовленістю в коді.
    """

    __tablename__ = "odds_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"))
    bookmaker_id: Mapped[int] = mapped_column(ForeignKey("bookmakers.id"))
    market_code: Mapped[str] = mapped_column(String(32))   # TOTALS | TEAM_TOTALS | BTTS ...
    selection: Mapped[str] = mapped_column(String(32))     # OVER | UNDER
    line: Mapped[float | None] = mapped_column(Float)
    odds: Mapped[float] = mapped_column(Float)
    #: Походження саме цього рядка. Глобального прапорця «база синтетична»
    #: недостатньо: у змішаній live/replay базі він бреше (ТЗ §1).
    data_source: Mapped[str] = mapped_column(String(32), default="LIVE")
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    fixture: Mapped[Fixture] = relationship(lazy="joined")
    bookmaker: Mapped[Bookmaker] = relationship(lazy="joined")

    __table_args__ = (
        Index(
            "ix_odds_snapshots_lookup",
            "fixture_id", "bookmaker_id", "market_code", "selection", "line", "source_timestamp",
        ),
    )


class ModelRun(Base):
    __tablename__ = "model_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"))
    model_version: Mapped[str] = mapped_column(String(64))
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lambda_home: Mapped[float] = mapped_column(Float)
    lambda_away: Mapped[float] = mapped_column(Float)
    lambda_total: Mapped[float] = mapped_column(Float)
    market_baseline_used: Mapped[bool] = mapped_column(Boolean, default=False)
    data_quality_score: Mapped[float | None] = mapped_column(Float)
    #: Походження даних, на яких порахований прогін: LIVE | SYNTHETIC_REPLAY | MIXED.
    data_source: Mapped[str] = mapped_column(String(32), default="LIVE")
    #: Повний snapshot входів — prediction має бути відтворюваною (ТЗ §51).
    inputs_json: Mapped[dict] = mapped_column(JSONB, default=dict)


class MarketPrediction(Base):
    __tablename__ = "market_predictions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    model_run_id: Mapped[int] = mapped_column(ForeignKey("model_runs.id"))
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"))
    bookmaker_id: Mapped[int] = mapped_column(ForeignKey("bookmakers.id"))
    market_code: Mapped[str] = mapped_column(String(32))
    selection: Mapped[str] = mapped_column(String(32))
    line: Mapped[float | None] = mapped_column(Float)
    bookmaker_odds: Mapped[float] = mapped_column(Float)
    market_probability: Mapped[float | None] = mapped_column(Float)
    model_probability: Mapped[float] = mapped_column(Float)
    fair_odds: Mapped[float | None] = mapped_column(Float)
    expected_value: Mapped[float] = mapped_column(Float)
    decision: Mapped[str] = mapped_column(String(16))
    reason_codes_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
