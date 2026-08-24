"""Задачі в Notion: створення, зміна статусу, читання для звіту.

Працюємо напряму з HTTP API, без окремого SDK — потрібно пʼять викликів,
і зайва залежність тут коштувала б більше, ніж економила.

Назви полів і значення статусів беремо з models.py: вони мають збігатися з
базою «Домовленості» символ у символ, тому лежать в одному місці.
"""
from __future__ import annotations

from typing import Any

import requests

from .config import NOTION_VERSION
from .models import (
    F_DEADLINE, F_PRIORITY, F_PROJECT, F_QUOTE, F_RESPONSIBLE, F_SOURCE,
    F_STATUS, F_TASK, HIGH, LOW, MEDIUM, NOT_STARTED, PRIORITIES, STATUSES,
    Commitment,
)

API = "https://api.notion.com/v1"

# Ліміт Notion на один текстовий фрагмент. Довший текст сервер відхиляє
# помилкою валідації, тому ріжемо на нашому боці.
MAX_TEXT = 2000

# Кольори опцій для бази, яку створюємо з нуля.
_PRIORITY_COLORS = {HIGH: "red", MEDIUM: "yellow", LOW: "green"}
_STATUS_COLORS = {
    "Не почато": "gray", "В роботі": "blue", "Готово": "green",
    "Відкладено": "yellow", "Скасовано": "red",
}


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


def clean_option(name: str) -> str:
    """Готує назву опції до запису.

    Notion забороняє кому в назві опції select і multi_select — запис із нею
    відхиляється. Модель же цілком може повернути «Продажі, Маркетинг» одним
    рядком, тому кому міняємо на риску, а не втрачаємо на цьому задачу.
    """
    return (name or "").replace(",", " /").strip()


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
        # Що довелося змінити на льоту, щоб задача все-таки записалась.
        # Конвеєр забирає ці рядки в журнал збоїв: мовчки псувати дані не можна.
        self.notes: list[str] = []
        # Опції поля «Проєкт» із бази. None — ще не читали.
        self._project_options: set[str] | None = None
        # Опції, які вже не вдалося завести. Нарада — це десяток задач із тим
        # самим проєктом: без цього кожна повторювала б безнадійний запит.
        self._failed_options: set[str] = set()

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
                "базі не видано доступ інтеграції: відкрийте сторінку бази, «...» → "
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

    # ── опції поля «Проєкт» ──────────────────────────────────────────
    # Notion відхиляє запис, якщо в multi_select приходить опція, якої немає
    # в базі: «Invalid multi_select value for property "Проєкт"». А модель
    # щоразу називає проєкт із контексту наради й рано чи пізно назве новий.
    # Тому перед записом опцію заводимо, а якщо не вийшло — пишемо задачу без
    # поля «Проєкт». Втратити задачу через назву проєкту не можна.
    def project_options(self, *, refresh: bool = False) -> set[str]:
        if self._project_options is None or refresh:
            data = self._request("GET", f"/databases/{self.database_id}")
            prop = (data.get("properties") or {}).get(F_PROJECT) or {}
            options = (prop.get("multi_select") or {}).get("options") or []
            self._project_options = {
                option.get("name", "") for option in options if option.get("name")
            }
        return self._project_options

    def ensure_project_option(self, name: str) -> bool:
        """Заводить опцію «Проєкт», якщо її ще немає. True — можна писати.

        Кожна невдача запамʼятовується: якщо інтеграції не видали право Update,
        безнадійний запит має піти один раз на прогін, а не на кожну задачу.
        """
        name = clean_option(name)
        if not name or name in self._failed_options:
            return False
        try:
            existing = self.project_options()
        except NotionError as exc:
            self.notes.append(f"не вдалося прочитати опції поля «{F_PROJECT}»: {exc}")
            self._failed_options.add(name)
            return False
        if name in existing:
            return True

        # Notion замінює список опцій цілком, тому надсилаємо старі разом із новою:
        # інакше PATCH стер би проєкти, які вже стоять на попередніх задачах.
        options = [{"name": option} for option in sorted(existing)] + [{"name": name}]
        try:
            self._request(
                "PATCH", f"/databases/{self.database_id}",
                {"properties": {F_PROJECT: {"multi_select": {"options": options}}}},
            )
        except NotionError as exc:
            self.notes.append(f"не вдалося додати опцію «{name}» у поле «{F_PROJECT}»: {exc}")
            self._failed_options.add(name)
            return False
        existing.add(name)
        return True

    def _priority(self, commitment: Commitment) -> str:
        """Пріоритет із закритого набору. Набір наш, тому звіряємо в коді."""
        if commitment.priority in PRIORITIES:
            return commitment.priority
        self.notes.append(
            f"«{commitment.task}»: невідомий пріоритет «{commitment.priority}» — "
            f"записано як «{MEDIUM}»"
        )
        return MEDIUM

    # ── створення й оновлення ────────────────────────────────────────
    def create_task(self, commitment: Commitment) -> str:
        """Створює задачу й повертає page_id, потрібний для кнопок у Telegram."""
        properties: dict[str, Any] = {
            F_TASK: {"title": _text(commitment.task)},
            F_RESPONSIBLE: {"rich_text": _text(commitment.responsible)},
            F_PRIORITY: {"select": {"name": self._priority(commitment)}},
            F_STATUS: {"select": {"name": NOT_STARTED}},
            F_SOURCE: {"rich_text": _text(commitment.source)},
            F_QUOTE: {"rich_text": _text(commitment.quote)},
        }
        if commitment.has_deadline:
            # Дата йде структурою, а не рядком: {"date": {"start": "2026-08-21"}}.
            properties[F_DEADLINE] = {"date": {"start": commitment.deadline}}
        if commitment.project:
            if self.ensure_project_option(commitment.project):
                properties[F_PROJECT] = {
                    "multi_select": [{"name": clean_option(commitment.project)}]
                }
            else:
                self.notes.append(
                    f"«{commitment.task}»: записано без поля «{F_PROJECT}» "
                    f"(«{commitment.project}») — задача на місці, проєкт проставте руками"
                )

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
            {"properties": {F_STATUS: {"select": {"name": status}}}},
        )

    def take_notes(self) -> list[str]:
        """Забирає накопичені зауваження й очищає список."""
        notes, self.notes = self.notes, []
        return notes

    # ── читання для звіту ────────────────────────────────────────────
    def query_tasks(self, source: str = "") -> list[dict]:
        """Усі задачі бази або лише з однієї наради. Гортає всі сторінки."""
        payload: dict[str, Any] = {"page_size": 100}
        if source:
            payload["filter"] = {"property": F_SOURCE, "rich_text": {"contains": source}}

        rows: list[dict] = []
        while True:
            data = self._request("POST", f"/databases/{self.database_id}/query", payload)
            for page in data.get("results", []):
                props = page.get("properties", {})
                rows.append(
                    {
                        "page_id": page.get("id", ""),
                        "task": _read_text(props.get(F_TASK)),
                        "responsible": _read_text(props.get(F_RESPONSIBLE)),
                        "status": _read_text(props.get(F_STATUS)) or NOT_STARTED,
                        "deadline": _read_text(props.get(F_DEADLINE)),
                        "priority": _read_text(props.get(F_PRIORITY)),
                        "project": _read_text(props.get(F_PROJECT)),
                        "source": _read_text(props.get(F_SOURCE)),
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
                    F_TASK: {"title": {}},
                    F_RESPONSIBLE: {"rich_text": {}},
                    F_DEADLINE: {"date": {}},
                    F_PRIORITY: {"select": {"options": [
                        {"name": name, "color": _PRIORITY_COLORS[name]}
                        for name in PRIORITIES
                    ]}},
                    F_STATUS: {"select": {"options": [
                        {"name": name, "color": _STATUS_COLORS[name]}
                        for name in STATUSES
                    ]}},
                    F_PROJECT: {"multi_select": {}},
                    F_SOURCE: {"rich_text": {}},
                    F_QUOTE: {"rich_text": {}},
                },
            },
        )
        database_id = data.get("id", "")
        if not database_id:
            raise NotionError("Notion не повернув id створеної бази")
        self.database_id = database_id
        self._project_options = set()
        return database_id

    def describe(self) -> str:
        return f"Notion, база {self.database_id}"
