"""Структури даних монітора."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Project:
    """Замовлення з біржі, зведене до спільного вигляду."""

    source: str                 # "freelancehunt"
    project_id: str
    title: str
    description: str
    url: str
    skills: list[str] = field(default_factory=list)
    budget_amount: float | None = None
    budget_currency: str = ""
    bid_count: int = 0
    employer: str = ""
    published_at: datetime | None = None
    is_only_for_plus: bool = False

    @property
    def budget_text(self) -> str:
        if self.budget_amount is None:
            return "бюджет не вказано"
        return f"{self.budget_amount:,.0f} {self.budget_currency}".replace(",", " ")

    @property
    def searchable(self) -> str:
        return " ".join([self.title, self.description, " ".join(self.skills)]).lower()


@dataclass
class Score:
    """Наскільки замовлення нам підходить."""

    total: int
    matched: list[str] = field(default_factory=list)
    product: str = ""           # який із наших продуктів пропонувати
    blocked_by: str = ""        # причина відсіву, якщо є

    @property
    def passed(self) -> bool:
        return not self.blocked_by


@dataclass
class Draft:
    """Готова заготовка відгуку."""

    opening: str = ""
    package: str = ""
    price: str = ""
    question: str = ""
    note: str = ""
    error: str = ""


@dataclass
class Lead:
    project: Project
    score: Score
    draft: Draft | None = None
