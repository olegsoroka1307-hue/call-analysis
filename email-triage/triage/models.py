"""Структури даних, якими обмінюються модулі конвеєра."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

Label = Literal["IMPORTANT", "NOT_IMPORTANT", "NEEDS_HUMAN_REVIEW"]

LABELS: tuple[str, ...] = ("IMPORTANT", "NOT_IMPORTANT", "NEEDS_HUMAN_REVIEW")


@dataclass
class Attachment:
    filename: str
    mime_type: str
    size: int
    data: bytes | None = None


@dataclass
class ExtractedDoc:
    """Текст, витягнутий із PDF або веб-сторінки."""
    source: str          # імʼя файлу або URL
    kind: str            # "pdf" | "web"
    text: str
    truncated: bool = False
    error: str | None = None


@dataclass
class EmailMessage:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    received_at: datetime
    attachments: list[Attachment] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def short(self) -> str:
        return f"{self.message_id} <{self.sender}> {self.subject[:60]!r}"


@dataclass
class Verdict:
    """Рішення класифікатора по одному листу."""
    label: str
    confidence: float
    reason: str
    sender_summary: str = ""
    topic: str = ""
    action_required: str = ""
    deadline: str = ""
    injection_suspected: bool = False
    degraded: bool = False       # True, якщо вердикт поставлено не моделлю (помилка/захист)


@dataclass
class TriageResult:
    email: EmailMessage
    verdict: Verdict
    docs: list[ExtractedDoc] = field(default_factory=list)
    processed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_row(self) -> list[str]:
        """Один рядок для Google Sheets / CSV. Порядок колонок = SHEET_HEADER."""
        e, v = self.email, self.verdict
        return [
            self.processed_at.strftime("%Y-%m-%d %H:%M:%S"),
            e.received_at.strftime("%Y-%m-%d %H:%M:%S"),
            v.label,
            f"{v.confidence:.2f}",
            e.sender,
            e.subject,
            v.sender_summary,
            v.topic,
            v.action_required,
            v.deadline,
            v.reason,
            "так" if v.injection_suspected else "",
            "так" if v.degraded else "",
            ", ".join(f"{d.kind}:{d.source}" for d in self.docs),
            e.message_id,
        ]


SHEET_HEADER: list[str] = [
    "Оброблено", "Отримано", "Вердикт", "Впевненість", "Відправник", "Тема",
    "Хто написав", "Про що", "Що зробити", "Дедлайн", "Чому такий вердикт",
    "Підозра на маніпуляцію", "Знижена якість", "Вкладення/посилання", "ID листа",
]
