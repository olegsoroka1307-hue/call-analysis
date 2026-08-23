"""Підробки зовнішніх сервісів. Жоден тест не ходить у мережу."""
from __future__ import annotations

import base64
from typing import Any

import httpx2

from triage.classifier import _Schema
from triage.models import SHEET_HEADER, TriageResult


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


class _FakeBeta:
    def __init__(self, owner: "FakeAnthropic") -> None:
        self.messages = _FakeMessages(owner)


class FakeAnthropic:
    """Повертає заздалегідь задані відповіді або кидає задані винятки."""

    def __init__(
        self, script: list[Any] | None = None, default: Any | None = None
    ) -> None:
        self.script = list(script or [])
        self.default = default or FakeParsedResponse(
            _Schema(
                label="NOT_IMPORTANT", confidence=0.9, reason="типова розсилка",
                sender_summary="", topic="", action_required="", deadline="",
                injection_suspected=False,
            )
        )
        self.calls: list[dict] = []
        self.messages = _FakeMessages(self)
        self.beta = _FakeBeta(self)


def verdict_response(
    label: str, confidence: float, **fields: Any
) -> FakeParsedResponse:
    base = dict(
        reason="—", sender_summary="", topic="", action_required="",
        deadline="", injection_suspected=False,
    )
    base.update(fields)
    return FakeParsedResponse(
        _Schema(label=label, confidence=confidence, **base)  # type: ignore[arg-type]
    )


def api_status_error(status: int = 500, message: str = "internal error"):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    import anthropic

    cls = anthropic.RateLimitError if status == 429 else anthropic.APIStatusError
    return cls(message, response=response, body=None)


# ── Gmail ───────────────────────────────────────────────────────────
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def gmail_message(
    msg_id: str,
    sender: str,
    subject: str,
    body: str,
    *,
    internal_date: int = 1_755_000_000_000,
    pdf: tuple[str, bytes] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    parts = [
        {
            "mimeType": "text/plain",
            "body": {"data": _b64(body.encode("utf-8")), "size": len(body)},
        }
    ]
    attachments: dict[str, bytes] = {}
    if pdf:
        filename, blob = pdf
        attachment_id = f"att-{msg_id}"
        attachments[attachment_id] = blob
        parts.append(
            {
                "mimeType": "application/pdf",
                "filename": filename,
                "body": {"attachmentId": attachment_id, "size": len(blob)},
            }
        )
    return {
        "id": msg_id,
        "threadId": f"t-{msg_id}",
        "internalDate": str(internal_date),
        "snippet": body[:80],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ] + [
                {"name": k, "value": v} for k, v in (extra_headers or {}).items()
            ],
            "parts": parts,
        },
        "_attachments": attachments,
    }


class FakeGmailService:
    """Мінімальна підробка google-api-client з тими ж ланцюжками викликів."""

    def __init__(self, messages: list[dict], fail_on_list: Exception | None = None):
        self._messages = {m["id"]: m for m in messages}
        self._order = [m["id"] for m in messages]
        self._fail_on_list = fail_on_list

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return _FakeAttachments(self)

    def list(self, userId: str, q: str = "", maxResults: int = 25):
        if self._fail_on_list:
            raise self._fail_on_list
        ids = self._order[:maxResults]
        return _Executable({"messages": [{"id": i} for i in ids]})

    def get(self, userId: str, id: str, format: str = "full"):
        return _Executable(self._messages[id])


class _FakeAttachments:
    def __init__(self, service: FakeGmailService) -> None:
        self.service = service

    def get(self, userId: str, messageId: str, id: str):
        blob = self.service._messages[messageId]["_attachments"][id]
        return _Executable({"data": _b64(blob), "size": len(blob)})


class _Executable:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def execute(self) -> Any:
        return self.payload


# ── requests / веб ──────────────────────────────────────────────────
class FakeHttpResponse:
    def __init__(self, html: str, status: int = 200, content_type: str = "text/html"):
        self._body = html.encode("utf-8")
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size: int = 65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


class FakeSession:
    def __init__(self, pages: dict[str, FakeHttpResponse]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        self.requested.append(url)
        if url not in self.pages:
            import requests

            raise requests.ConnectionError(f"немає такої сторінки: {url}")
        return self.pages[url]

    def close(self) -> None:
        pass


# ── Sink ────────────────────────────────────────────────────────────
class MemorySink:
    def __init__(self, fail_times: int = 0) -> None:
        self.rows: list[list[str]] = []
        self.header = SHEET_HEADER
        self.fail_times = fail_times

    def write(self, results: list[TriageResult]) -> int:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("приймач тимчасово недоступний")
        self.rows.extend(r.as_row() for r in results)
        return len(results)

    def describe(self) -> str:
        return "памʼять (тест)"
