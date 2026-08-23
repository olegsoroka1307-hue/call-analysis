"""Куди йде знайдене: консоль, Telegram, CSV."""
from __future__ import annotations

import csv
from pathlib import Path

import requests

from .models import Lead
from .scoring import product_label

TELEGRAM_LIMIT = 4000

CSV_HEADER = [
    "Знайдено", "Бали", "Продукт", "Назва", "Бюджет", "Відгуків",
    "Опубліковано", "Посилання", "Слова-збіги", "Перше речення",
    "Пакет", "Ціна", "Питання", "Застереження",
]


def format_lead(lead: Lead, *, with_draft: bool = True) -> str:
    p, s, d = lead.project, lead.score, lead.draft
    published = p.published_at.strftime("%d.%m %H:%M") if p.published_at else "—"
    lines = [
        f"🎯 {p.title}",
        "",
        f"💰 {p.budget_text}   ·   відгуків: {p.bid_count}   ·   {published}",
        f"📊 релевантність {s.total} · {product_label(s.product)}",
        f"🔑 {', '.join(s.matched[:8])}",
        "",
        p.url,
    ]
    if with_draft and d and not d.error:
        lines += [
            "",
            "─── ЧЕРНЕТКА ВІДГУКУ ───",
            "",
            d.opening,
            "",
            f"Пакет: {d.package}",
            f"Ціна: {d.price}",
            "",
            f"Питання: {d.question}",
        ]
        if d.note:
            lines += ["", f"⚠️ Для вас, не в відгук: {d.note}"]
    elif with_draft and d and d.error:
        lines += ["", f"(чернетку не склали: {d.error})"]
    return "\n".join(lines)


class ConsoleNotifier:
    name = "консоль"

    def send(self, lead: Lead) -> None:
        print("\n" + "═" * 70)
        print(format_lead(lead))
        print("═" * 70)


class TelegramNotifier:
    name = "Telegram"

    def __init__(
        self, token: str, chat_id: str, session: requests.Session | None = None
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.session = session or requests.Session()

    def send(self, lead: Lead) -> None:
        text = format_lead(lead)
        if len(text) > TELEGRAM_LIMIT:
            text = text[:TELEGRAM_LIMIT] + "\n…"
        response = self.session.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Telegram відхилив повідомлення ({response.status_code}). "
                "Найчастіша причина — ви ще не написали боту /start."
            )


class CsvSink:
    name = "CSV"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, lead: Lead) -> None:
        from datetime import datetime, timezone

        p, s, d = lead.project, lead.score, lead.draft
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(CSV_HEADER)
            writer.writerow([
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                s.total,
                product_label(s.product),
                p.title,
                p.budget_text,
                p.bid_count,
                p.published_at.strftime("%Y-%m-%d %H:%M") if p.published_at else "",
                p.url,
                ", ".join(s.matched[:10]),
                d.opening if d else "",
                d.package if d else "",
                d.price if d else "",
                d.question if d else "",
                (d.note or d.error) if d else "",
            ])
