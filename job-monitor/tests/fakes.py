"""Підробки Freelancehunt, Anthropic і Telegram. Мережа не потрібна."""
from __future__ import annotations

from typing import Any

import httpx2

from monitor.drafter import _Schema
from monitor.models import Lead


def fh_item(
    project_id: int,
    name: str,
    description: str,
    *,
    skills: list[str] | None = None,
    amount: int | None = 12000,
    currency: str = "UAH",
    bid_count: int = 3,
    only_for_plus: bool = False,
    published_at: str = "2026-08-23T09:15:00+03:00",
) -> dict:
    """Один запис у форматі JSON:API, як його віддає Freelancehunt."""
    return {
        "id": project_id,
        "type": "project",
        "attributes": {
            "name": name,
            "description": description,
            "description_html": f"<p>{description}</p>",
            "skills": [{"id": i, "name": s} for i, s in enumerate(skills or [], 1)],
            "status": {"id": 11, "name": "Open for proposals"},
            "budget": None if amount is None else {"amount": amount, "currency": currency},
            "bid_count": bid_count,
            "is_remote_job": True,
            "is_only_for_plus": only_for_plus,
            "employer": {"id": 1, "login": "client", "first_name": "Олег", "last_name": "К."},
            "published_at": published_at,
            "expired_at": "2026-08-30T09:15:00+03:00",
        },
        "links": {
            "self": {
                "api": f"https://api.freelancehunt.com/v2/projects/{project_id}",
                "web": f"https://freelancehunt.com/project/test/{project_id}.html",
            }
        },
    }


class FakeHttpResponse:
    def __init__(self, payload: Any, status: int = 200, headers: dict | None = None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Підробка requests.Session для клієнта Freelancehunt і Telegram."""

    def __init__(self, pages: list[Any] | None = None, post_status: int = 200):
        self.pages = list(pages or [])
        self.calls: list[dict] = []
        self.posts: list[dict] = []
        self.post_status = post_status

    def get(self, url: str, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "params": params or {}})
        if not self.pages:
            return FakeHttpResponse({"data": []})
        item = self.pages.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, FakeHttpResponse):
            return item
        return FakeHttpResponse({"data": item})

    def post(self, url: str, json=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        return FakeHttpResponse({"ok": True}, status=self.post_status)


# ── Anthropic ───────────────────────────────────────────────────
class FakeParsed:
    def __init__(self, parsed, stop_reason="end_turn"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, owner):
        self.owner = owner

    def parse(self, **kwargs):
        self.owner.calls.append(kwargs)
        item = self.owner.script.pop(0) if self.owner.script else self.owner.default
        if isinstance(item, Exception):
            raise item
        return item


class FakeAnthropic:
    def __init__(self, script=None, default=None):
        self.script = list(script or [])
        self.calls: list[dict] = []
        self.default = default or draft_response()
        self.messages = _FakeMessages(self)


def draft_response(
    opening="У вас щотижня зривається половина домовленостей, бо їх ніхто не фіксує.",
    package="Команда",
    price="10 000 ₴, 4 дні",
    question="Де зараз живуть задачі — Notion, таблиця, чи ніде?",
    note="",
    fits="yes",
):
    return FakeParsed(
        _Schema(
            opening=opening, package=package, price=price,
            question=question, note=note, fits=fits,
        )
    )


def api_error(status: int = 500):
    import anthropic

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "boom", response=httpx2.Response(status, request=request), body=None
    )


class MemoryNotifier:
    name = "памʼять"

    def __init__(self, fail_times: int = 0):
        self.sent: list[Lead] = []
        self.fail_times = fail_times

    def send(self, lead: Lead) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("канал тимчасово недоступний")
        self.sent.append(lead)
