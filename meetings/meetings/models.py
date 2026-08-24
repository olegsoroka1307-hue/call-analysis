"""Типи, якими обмінюються частини системи."""
from __future__ import annotations

from dataclasses import dataclass, field

# Статуси в Notion. Значення мають збігатися з назвами опцій у базі
# «Домовленості» символ у символ, інакше Notion відхилить запис.
NOT_STARTED = "Не почато"
IN_PROGRESS = "В роботі"
DONE = "Готово"
POSTPONED = "Відкладено"
CANCELLED = "Скасовано"

STATUSES = (NOT_STARTED, IN_PROGRESS, DONE, POSTPONED, CANCELLED)

HIGH = "Високий"
MEDIUM = "Середній"
LOW = "Низький"

PRIORITIES = (HIGH, MEDIUM, LOW)

# Назви колонок у базі. Тримаємо в одному місці: якщо клієнт перейменує
# поле у себе, правка потрібна лише тут.
F_TASK = "Задача"
F_RESPONSIBLE = "Відповідальний"
F_DEADLINE = "Дедлайн"
F_PRIORITY = "Пріоритет"
F_STATUS = "Статус"
F_PROJECT = "Проєкт"
F_SOURCE = "Джерело"
F_QUOTE = "Цитата"


@dataclass
class Commitment:
    """Одна домовленість, знята з транскрипції."""

    responsible: str
    task: str
    deadline: str = ""          # ISO-дата або порожньо, якщо не називали
    priority: str = MEDIUM
    project: str = ""
    quote: str = ""             # дослівна цитата з розмови
    source: str = ""            # «Назва зустрічі — дата»
    page_id: str = ""           # заповнюється після запису в Notion

    @property
    def has_deadline(self) -> bool:
        return bool(self.deadline.strip())


@dataclass
class Employee:
    """Співробітник у реєстрі бота."""

    name: str
    chat_id: int
    telegram_username: str = ""


@dataclass
class Delivery:
    """Результат розсилки по одній людині."""

    name: str
    chat_id: int | None
    sent: int = 0
    error: str = ""

    @property
    def reached(self) -> bool:
        return self.chat_id is not None and not self.error


@dataclass
class MeetingResult:
    """Підсумок розбору однієї зустрічі."""

    meeting: str
    commitments: list[Commitment] = field(default_factory=list)
    deliveries: list[Delivery] = field(default_factory=list)
    unclear: list[str] = field(default_factory=list)
    created_in_notion: int = 0
    skipped_duplicate: bool = False
    degraded: bool = False
    degraded_reason: str = ""

    @property
    def unreached(self) -> list[Delivery]:
        return [d for d in self.deliveries if not d.reached]

    def lines(self) -> list[str]:
        out = [f"Зустріч: {self.meeting}"]
        if self.skipped_duplicate:
            out.append("  вже розбиралася раніше — нічого не створено")
            return out
        out.append(f"  домовленостей знайдено: {len(self.commitments)}")
        out.append(f"  створено задач у Notion: {self.created_in_notion}")
        reached = [d for d in self.deliveries if d.reached]
        out.append(
            "  розіслано в Telegram: "
            + (", ".join(f"{d.name} ({d.sent})" for d in reached) or "нікому")
        )
        for d in self.unreached:
            out.append(f"  НЕ дійшло до {d.name}: {d.error or 'немає в реєстрі бота'}")
        for question in self.unclear:
            out.append(f"  ПОТРІБНА ВАША ВІДПОВІДЬ: {question}")
        if self.degraded:
            out.append(f"  УВАГА: {self.degraded_reason}")
        return out


@dataclass
class ReportRow:
    """Рядок звіту «що обіцяли — що зробили»."""

    responsible: str
    task: str
    status: str
    deadline: str = ""
    overdue_days: int = 0
