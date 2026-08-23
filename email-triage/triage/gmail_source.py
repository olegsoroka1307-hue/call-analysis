"""Читання пошти через Gmail API.

Scope навмисно один — gmail.readonly. Через нього фізично неможливо видалити,
надіслати або переслати листа: сервер відхилить такий запит незалежно від коду.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import GMAIL_READONLY_SCOPE, Config, ConfigError
from .models import Attachment, EmailMessage


def _assert_readonly(scopes: list[str]) -> None:
    forbidden = [s for s in scopes if "gmail" in s and not s.endswith("gmail.readonly")]
    if forbidden:
        raise ConfigError(
            "SAFE MODE: дозволений лише gmail.readonly, а запитано "
            f"{forbidden}. Це заблоковано навмисно."
        )


def authorize(cfg: Config) -> Credentials:
    """Одноразовий вхід у Google. Відкриває браузер власника акаунта."""
    _assert_readonly(cfg.scopes)
    token_path = Path(cfg.token_file)
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), cfg.scopes)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        cred_path = Path(cfg.credentials_file)
        if not cred_path.exists():
            raise ConfigError(
                f"Немає файлу {cred_path}. Це файл OAuth-клієнта з Google Cloud "
                "Console — інструкція в README, розділ «Що потрібно від вас»."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), cfg.scopes)
        creds = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    return creds


def _decode(data: str | None) -> bytes:
    if not data:
        return b""
    return base64.urlsafe_b64decode(data.encode("utf-8"))


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _walk_parts(payload: dict) -> list[dict]:
    parts = [payload]
    queue = list(payload.get("parts") or [])
    while queue:
        part = queue.pop(0)
        parts.append(part)
        queue.extend(part.get("parts") or [])
    return parts


class GmailSource:
    """Джерело листів. Уміє тільки читати."""

    def __init__(self, cfg: Config, service=None) -> None:
        self.cfg = cfg
        _assert_readonly(cfg.scopes)
        if service is not None:
            self.service = service
        else:
            self.service = build(
                "gmail", "v1", credentials=authorize(cfg), cache_discovery=False
            )

    def fetch(self, limit: int | None = None) -> list[EmailMessage]:
        limit = limit or self.cfg.max_emails_per_run
        listing = (
            self.service.users()
            .messages()
            .list(userId="me", q=self.cfg.gmail_query, maxResults=limit)
            .execute()
        )
        out: list[EmailMessage] = []
        for ref in listing.get("messages", []) or []:
            raw = (
                self.service.users()
                .messages()
                .get(userId="me", id=ref["id"], format="full")
                .execute()
            )
            out.append(self._parse(raw))
        return out

    def _parse(self, raw: dict) -> EmailMessage:
        payload = raw.get("payload", {}) or {}
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in payload.get("headers", []) or []
        }
        text_parts: list[str] = []
        html_parts: list[str] = []
        attachments: list[Attachment] = []

        for part in _walk_parts(payload):
            mime = part.get("mimeType", "")
            body = part.get("body", {}) or {}
            filename = part.get("filename") or ""
            if filename and (body.get("attachmentId") or body.get("data")):
                data = b""
                if body.get("attachmentId"):
                    data = self._attachment_bytes(raw["id"], body["attachmentId"])
                elif body.get("data"):
                    data = _decode(body["data"])
                attachments.append(
                    Attachment(
                        filename=filename,
                        mime_type=mime,
                        size=int(body.get("size") or len(data)),
                        data=data,
                    )
                )
            elif mime == "text/plain" and body.get("data"):
                text_parts.append(_decode(body["data"]).decode("utf-8", "replace"))
            elif mime == "text/html" and body.get("data"):
                html_parts.append(_decode(body["data"]).decode("utf-8", "replace"))

        body_text = "\n".join(p for p in text_parts if p.strip()).strip()
        if not body_text and html_parts:
            body_text = _html_to_text("\n".join(html_parts))
        if not body_text:
            body_text = raw.get("snippet", "") or ""

        ts = raw.get("internalDate")
        received = (
            datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
            if ts
            else datetime.now(timezone.utc)
        )
        return EmailMessage(
            message_id=raw.get("id", ""),
            thread_id=raw.get("threadId", ""),
            sender=headers.get("from", "(невідомо)"),
            subject=headers.get("subject", "(без теми)"),
            body=body_text,
            received_at=received,
            attachments=attachments,
            headers=headers,
        )

    def _attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
        blob = (
            self.service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        return _decode(blob.get("data"))
