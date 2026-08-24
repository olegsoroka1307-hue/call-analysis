"""Задачі в Notion: створення, зміна статусу, читання для звіту.

Працюємо напряму з HTTP API, без окремого SDK — потрібні чотири виклики,
і зайва залежність тут коштувала б більше, ніж економила.
"""
from __future__ import annotations

from typing import Any

import requests

from .config import NOTION_VERSION
from .models import NOT_STARTED, STATUSES, Commitment

API = "https://api.notion.com/v1"

# Ліміт Notion на один текстовий фрагмент. Довший текст сервер відхиляє
# помилкою валідації, тому ріжемо на нашому боці.
MAX_TEXT = 2000

TITLE = "Задача"
RESPONSIBLE = "Ответственный"
DEADLINE = "Дедлайн"
PRIORITY = "Приоритет"
STATUS = "Статус"
PROJECT = "Проект"
SOURCE = "Источник встречи"
QUOTE = "Цитата"


class NotionError(RuntimeError):
    pass


def _text(value: str) -> list[dict]:
    value = (value or "").strip()
    if not value:
        return []
    if len(value) > MAX_TEXT:
        value = value[: MAX_TEXT - 1] + "…"
    return [{"type": "text", "text": {"content": value}}]


def _read_text(prop: dict | None) -> str:
    if not prop:
        return ""
    kind = prop.get("type")
    if kind in ("title", "rich_text"):
        return "".join(part.get("plain_text", "") for part in prop.get(kind, []))
    if kind == "select":
        selected = prop.get("select")
        return selected.get("name", "") if selected else ""
    if kind == "multi_select":
        return ", ".join(item.get("name", "") for item in prop.get("multi_select", []))
    if kind == "date":
        value = prop.get("date")
        return value.get("start", "") if value else ""
    return ""


class NotionClient:
    def __init__(
        self,
        api_key: str,
        database_id: str,
        *,
        timeout: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        self.database_id = database_id
        self.timeout = timeout
        self.session = session or requests.Session()
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            response = self.session.request(
                method, f"{API}{path}", headers=self.headers,
                json=payload, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise NotionError(f"не вдалося зʼєднатися з Notion: {exc}") from exc

        if response.status_code >= 400:
            raise NotionError(self._explain(response))
        try:
            return response.json()
        except ValueError as exc:
            raise NotionError(f"Notion повернув не JSON: {response.text[:200]}") from exc

    def _explain(self, response: requests.Response) -> str:
        """Помилки Notion самі по собі незрозумілі — перекладаємо найчастіші."""
        try:
            body = response.json()
            message = body.get("message", response.text[:300])
            code = body.get("code", "")
        except ValueError:
            message, code = response.text[:300], ""

        if response.status_code == 401:
            return (
                "Notion не прийняв ключ (401). Перевірте NOTION_API_KEY у .env — "
                "це має бути внутрішній ключ інтеграції, він починається на secret_ "
                f"або ntn_. ({message})"
            )
        if response.status_code == 404 or code == "object_not_found":
            return (
                f"Notion не бачить базу {self.database_id} (404). Найчастіша причина — "
                "базою не поділилися з інтеграцією: відкрийте сторінку бази, «...» → "
                f"Connections → додайте свою інтеграцію. ({message})"
            )
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "кілька")
            return f"Notion просить зачекати {retry} с (429): {message}"
        if code == "validation_error":
            return (
                f"Notion відхилив дані (400): {message}. Зазвичай це означає, що "
                "у базі «Домовленості» поле названо інакше або має інший тип — "
                "звірте назви полів із README."
            )
        return f"Notion повернув помилку {response.status_code}: {message}"

    # ── створення й оновлення ────────────────────────────────────────
    def create_task(self, commitment: Commitment) -> str:
        """Створює задачу й повертає page_id, потрібний для кнопок у Telegram."""
        properties: dict[str, Any] = {
            TITLE: {"title": _text(commitment.task)},
            RESPONSIBLE: {"rich_text": _text(commitment.responsible)},
            PRIORITY: {"select": {"name": commitment.priority}},
            STATUS: {"select": {"name": NOT_STARTED}},
            SOURCE: {"rich_text": _text(commitment.source)},
            QUOTE: {"rich_text": _text(commitment.quote)},
        }
        if commitment.has_deadline:
            properties[DEADLINE] = {"date": {"start": commitment.deadline}}
        if commitment.project:
            properties[PROJECT] = {"multi_select": [{"name": commitment.project}]}

        data = self._request(
            "POST", "/pages",
            {"parent": {"database_id": self.database_id}, "properties": properties},
        )
        page_id = data.get("id", "")
        if not page_id:
            raise NotionError("Notion не повернув id створеної сторінки")
        return page_id

    def set_status(self, page_id: str, status: str) -> None:
        if status not in STATUSES:
            raise NotionError(f"невідомий статус {status!r}")
        self._request(
            "PATCH", f"/pages/{page_id}",
            {"properties": {STATUS: {"select": {"name": status}}}},
        )

    # ── читання для звіту ────────────────────────────────────────────
    def query_tasks(self, source: str = "") -> list[dict]:
        """Усі задачі бази або лише з однієї наради. Гортає всі сторінки."""
        payload: dict[str, Any] = {"page_size": 100}
        if source:
            payload["filter"] = {"property": SOURCE, "rich_text": {"contains": source}}

        rows: list[dict] = []
        while True:
            data = self._request("POST", f"/databases/{self.database_id}/query", payload)
            for page in data.get("results", []):
                props = page.get("properties", {})
                rows.append(
                    {
                        "page_id": page.get("id", ""),
                        "task": _read_text(props.get(TITLE)),
                        "responsible": _read_text(props.get(RESPONSIBLE)),
                        "status": _read_text(props.get(STATUS)) or NOT_STARTED,
                        "deadline": _read_text(props.get(DEADLINE)),
                        "priority": _read_text(props.get(PRIORITY)),
                        "project": _read_text(props.get(PROJECT)),
                        "source": _read_text(props.get(SOURCE)),
                    }
                )
            if not data.get("has_more"):
                return rows
            payload["start_cursor"] = data["next_cursor"]

    # ── перший запуск ────────────────────────────────────────────────
    def create_database(self, parent_page_id: str, title: str = "Домовленості") -> str:
        """Створює базу з потрібними полями на вказаній сторінці."""
        data = self._request(
            "POST", "/databases",
            {
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "title": _text(title),
                "properties": {
                    TITLE: {"title": {}},
                    RESPONSIBLE: {"rich_text": {}},
                    DEADLINE: {"date": {}},
                    PRIORITY: {"select": {"options": [
                        {"name": "Высокий", "color": "red"},
                        {"name": "Средний", "color": "yellow"},
                        {"name": "Низкий", "color": "green"},
                    ]}},
                    STATUS: {"select": {"options": [
                        {"name": "Не начата", "color": "gray"},
                        {"name": "В работе", "color": "blue"},
                        {"name": "Готово", "color": "green"},
                        {"name": "Отложено", "color": "yellow"},
                        {"name": "Отменена", "color": "red"},
                    ]}},
                    PROJECT: {"multi_select": {}},
                    SOURCE: {"rich_text": {}},
                    QUOTE: {"rich_text": {}},
                },
            },
        )
        database_id = data.get("id", "")
        if not database_id:
            raise NotionError("Notion не повернув id створеної бази")
        self.database_id = database_id
        return database_id

    def describe(self) -> str:
        return f"Notion, база {self.database_id}"
