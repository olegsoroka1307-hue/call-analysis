"""Підробки зовнішніх сервісів. Жоден тест не ходить у мережу."""
from __future__ import annotations

import json
from typing import Any

from meetings.extractor import _CommitmentSchema, _Schema
from meetings.models import Commitment, Delivery


# ── Anthropic ───────────────────────────────────────────────────────
class FakeParsedResponse:
    def __init__(self, parsed: _Schema | None, stop_reason: str = "end_turn") -> None:
        self.parsed_output = parsed
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, owner: "FakeAnthropic") -> None:
        self.owner = owner

    def parse(self, **kwargs: Any) -> FakeParsedResponse:
        self.owner.calls.append(kwargs)
        script = self.owner.script.pop(0) if self.owner.script else self.owner.default
        if isinstance(script, Exception):
            raise script
        return script


class FakeAnthropic:
    """Повертає заздалегідь задані відповіді або кидає задані винятки."""

    def __init__(self, script: list[Any] | None = None, default: Any | None = None) -> None:
        self.script = list(script or [])
        self.default = default or extraction()
        self.calls: list[dict] = []
        self.messages = _FakeMessages(self)


def commitment_schema(
    responsible: str = "Саша",
    task: str = "Надіслати КП Альфі",
    deadline: str = "2026-08-28",
    priority: str = "Високий",
    project: str = "Продажі",
    quote: str = "Я скину КП до четверга.",
) -> _CommitmentSchema:
    return _CommitmentSchema(
        responsible=responsible, task=task, deadline=deadline,
        priority=priority, project=project, quote=quote,
    )


def extraction(
    title: str = "Планірка",
    commitments: list[_CommitmentSchema] | None = None,
    unclear: list[str] | None = None,
    injection_suspected: bool = False,
    stop_reason: str = "end_turn",
) -> FakeParsedResponse:
    return FakeParsedResponse(
        _Schema(
            meeting_title=title,
            commitments=[commitment_schema()] if commitments is None else commitments,
            unclear=unclear or [],
            injection_suspected=injection_suspected,
        ),
        stop_reason=stop_reason,
    )


def api_status_error(status: int = 500, message: str = "internal error"):
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    cls = anthropic.RateLimitError if status == 429 else anthropic.APIStatusError
    return cls(message, response=response, body=None)


# ── HTTP (Notion, Telegram) ─────────────────────────────────────────
class FakeResponse:
    def __init__(self, status_code: int = 200, body: Any = None, headers: dict | None = None):
        self.status_code = status_code
        self._body = {} if body is None else body
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


def db_schema(options: list[str] | None = None, field: str = "Проєкт") -> FakeResponse:
    """Відповідь Notion на читання схеми бази: які опції має поле «Проєкт»."""
    return FakeResponse(200, {
        "id": "db-1",
        "properties": {
            field: {
                "type": "multi_select",
                "multi_select": {"options": [{"name": name} for name in (options or [])]},
            }
        },
    })


class FakeSession:
    """Віддає відповіді за скриптом і запамʼятовує кожен запит."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[dict] = []

    def _next(self, record: dict):
        self.requests.append(record)
        if not self.responses:
            return FakeResponse(200, {})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def request(self, method, url, headers=None, json=None, timeout=None):
        return self._next(
            {"method": method, "url": url, "headers": headers or {}, "json": json or {}}
        )

    def post(self, url, json=None, timeout=None):
        return self._next({"method": "POST", "url": url, "json": json or {}})


# ── Notion / Telegram на рівні обʼєктів ─────────────────────────────
class FakeNotion:
    def __init__(self, fail_on: set[str] | None = None,
                 notes: list[str] | None = None) -> None:
        self.created: list[Commitment] = []
        self.statuses: list[tuple[str, str]] = []
        self.fail_on = fail_on or set()
        self.counter = 0
        # Зауваження, які справжній клієнт лишає, коли задача записалась
        # не такою, якою її зняли з наради.
        self.notes: list[str] = list(notes or [])

    def create_task(self, commitment: Commitment) -> str:
        from meetings.notion import NotionError

        if commitment.task in self.fail_on:
            raise NotionError("тестовий збій Notion")
        self.created.append(commitment)
        self.counter += 1
        return f"{self.counter:032d}"

    def set_status(self, page_id: str, status: str) -> None:
        from meetings.notion import NotionError

        if page_id in self.fail_on:
            raise NotionError("тестовий збій Notion")
        self.statuses.append((page_id, status))

    def query_tasks(self, source: str = "") -> list[dict]:
        return []

    def take_notes(self) -> list[str]:
        notes, self.notes = self.notes, []
        return notes


class FakeTelegram:
    def __init__(self, updates: list[dict] | None = None, raise_on_send: bool = False):
        self.sent: list[tuple[int, str, dict | None]] = []
        self.answers: list[tuple[str, str]] = []
        self.cleared: list[tuple[int, int]] = []
        self.updates = list(updates or [])
        self.raise_on_send = raise_on_send

    def send(self, chat_id: int, text: str, keyboard: dict | None = None) -> None:
        from meetings.telegram import TelegramError

        if self.raise_on_send:
            raise TelegramError("тестовий збій Telegram")
        self.sent.append((chat_id, text, keyboard))

    def send_meeting_summary(self, meeting, commitments, registry) -> list[Delivery]:
        from meetings.telegram import TelegramClient

        return TelegramClient.send_meeting_summary(self, meeting, commitments, registry)

    def get_updates(self, offset=None, timeout: int = 0) -> list[dict]:
        updates, self.updates = self.updates, []
        return updates

    def answer_callback(self, callback_id: str, text: str) -> None:
        self.answers.append((callback_id, text))

    def clear_keyboard(self, chat_id: int, message_id: int) -> None:
        self.cleared.append((chat_id, message_id))


def callback_update(update_id: int, data: str, chat_id: int = 10, message_id: int = 5) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
        },
    }
