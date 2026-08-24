"""Розсилка задач у Telegram і обробка натискань на кнопки.

Кнопка під задачею — це єдине місце, де співробітник взагалі стикається з
системою. Тому натискання має спрацьовувати з першого разу й одразу міняти
статус у Notion: якщо людина натисне «Готово», а в звіті через тиждень
задача висітиме невиконаною, кнопками більше ніхто не скористається.
"""
from __future__ import annotations

import html
from collections import defaultdict

import requests

from .models import (
    CANCELLED, DONE, HIGH, LOW, MEDIUM, POSTPONED, Commitment, Delivery, Employee,
)

API = "https://api.telegram.org"

# Ліміт Telegram на текст повідомлення.
MAX_TEXT = 4096
# Ліміт на callback_data — 64 байти. Тому в кнопку йде дія одним символом
# і page_id без дефісів: 1 + 1 + 32 = 34 байти, із запасом.
ACTIONS = {"d": DONE, "p": POSTPONED, "x": CANCELLED}

PRIORITY_MARK = {HIGH: "🔴", MEDIUM: "🟡", LOW: "⚪"}


class TelegramError(RuntimeError):
    pass


def pack_callback(action: str, page_id: str) -> str:
    if action not in ACTIONS:
        raise TelegramError(f"невідома дія {action!r}")
    return f"{action}|{page_id.replace('-', '')}"


def unpack_callback(data: str) -> tuple[str, str]:
    """Повертає (статус, page_id без дефісів). Порожній статус — дані чужі."""
    action, _, page_id = (data or "").partition("|")
    return ACTIONS.get(action, ""), page_id


def task_message(commitment: Commitment) -> str:
    mark = PRIORITY_MARK.get(commitment.priority, "")
    lines = [f"<b>{html.escape(commitment.task)}</b>"]
    if commitment.deadline:
        lines.append(f"Термін: {html.escape(commitment.deadline)}")
    lines.append(f"Пріоритет: {mark} {html.escape(commitment.priority)}".strip())
    if commitment.project:
        lines.append(f"Напрям: {html.escape(commitment.project)}")
    if commitment.quote:
        lines.append("")
        lines.append(f"<i>«{html.escape(commitment.quote)}»</i>")
    text = "\n".join(lines)
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def task_keyboard(page_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Готово", "callback_data": pack_callback("d", page_id)},
            {"text": "🕐 Відкласти", "callback_data": pack_callback("p", page_id)},
            {"text": "❌ Не актуально", "callback_data": pack_callback("x", page_id)},
        ]]
    }


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        timeout: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self.session = session or requests.Session()

    def _call(self, method: str, payload: dict) -> dict:
        url = f"{API}/bot{self.token}/{method}"
        try:
            response = self.session.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise TelegramError(f"не вдалося зʼєднатися з Telegram: {exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(
                f"Telegram повернув не JSON: {response.text[:200]}"
            ) from exc
        if not body.get("ok"):
            raise TelegramError(self._explain(response.status_code, body))
        return body.get("result", {})

    def _explain(self, status: int, body: dict) -> str:
        description = body.get("description", "без пояснення")
        if status == 401:
            return (
                "Telegram не прийняв токен (401). Перевірте TELEGRAM_BOT_TOKEN у .env — "
                f"його видає @BotFather командою /newbot. ({description})"
            )
        if status == 403:
            return (
                "Бот не може написати цій людині (403). Так буває, поки вона сама "
                f"не надішле боту /start. ({description})"
            )
        if status == 429:
            retry = body.get("parameters", {}).get("retry_after", "кілька")
            return f"Telegram просить зачекати {retry} с (429): {description}"
        return f"Telegram повернув помилку {status}: {description}"

    # ── розсилка ─────────────────────────────────────────────────────
    def send(self, chat_id: int, text: str, keyboard: dict | None = None) -> None:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        self._call("sendMessage", payload)

    def send_meeting_summary(
        self, meeting: str, commitments: list[Commitment], registry
    ) -> list[Delivery]:
        """По одному повідомленню на задачу, з привітанням перед ними."""
        by_person: dict[str, list[Commitment]] = defaultdict(list)
        for commitment in commitments:
            by_person[commitment.responsible].append(commitment)

        deliveries: list[Delivery] = []
        for name, tasks in by_person.items():
            employee: Employee | None = registry.find(name)
            if employee is None:
                deliveries.append(
                    Delivery(
                        name=name, chat_id=None,
                        error="немає в реєстрі бота — хай напише боту /start",
                    )
                )
                continue

            delivery = Delivery(name=employee.name, chat_id=employee.chat_id)
            word = "задача" if len(tasks) == 1 else "задачі"
            try:
                self.send(
                    employee.chat_id,
                    f"Нарада <b>{html.escape(meeting)}</b> — за вами "
                    f"{len(tasks)} {word}.",
                )
            except TelegramError as exc:
                delivery.error = str(exc)
                deliveries.append(delivery)
                continue

            for commitment in tasks:
                try:
                    self.send(
                        employee.chat_id,
                        task_message(commitment),
                        task_keyboard(commitment.page_id) if commitment.page_id else None,
                    )
                    delivery.sent += 1
                except TelegramError as exc:
                    # Одна задача не дійшла — решта все одно має піти.
                    delivery.error = str(exc)
            deliveries.append(delivery)
        return deliveries

    # ── реєстрація ───────────────────────────────────────────────────
    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        payload: dict = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload) or []

    def collect_contacts(self, offset: int | None = None) -> list[dict]:
        """Хто написав боту: імʼя, username, chat_id — сировина для реєстру."""
        contacts: dict[int, dict] = {}
        for update in self.get_updates(offset):
            chat = (update.get("message") or {}).get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is None:
                continue
            name = " ".join(
                part for part in (chat.get("first_name"), chat.get("last_name")) if part
            ) or chat.get("username", "") or str(chat_id)
            contacts[chat_id] = {
                "chat_id": int(chat_id),
                "name": name,
                "username": chat.get("username", ""),
            }
        return list(contacts.values())

    # ── натискання кнопок ────────────────────────────────────────────
    def answer_callback(self, callback_id: str, text: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def clear_keyboard(self, chat_id: int, message_id: int) -> None:
        self._call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {}},
        )


def restore_uuid(compact: str) -> str:
    """Повертає дефіси в id сторінки: у callback_data вони не влізають."""
    if len(compact) != 32 or "-" in compact:
        return compact
    return "-".join(
        (compact[:8], compact[8:12], compact[12:16], compact[16:20], compact[20:])
    )
