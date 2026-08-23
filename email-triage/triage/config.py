"""Завантаження та перевірка конфігурації.

Власник редагує лише config.yaml простою мовою. Секрети живуть в .env
і в конфіг ніколи не потрапляють.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Єдиний дозволений scope Gmail. Розширювати не можна: саме він робить
# видалення й відправку листів технічно неможливими, а не лише забороненими.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    business_context: str
    gmail_query: str = "is:unread newer_than:7d"
    max_emails_per_run: int = 25
    output_language: str = "українська"

    model: str = "claude-opus-5"
    effort: str = "medium"
    confidence_threshold: float = 0.75
    max_body_chars: int = 12000
    max_doc_chars: int = 8000

    fetch_links: bool = True
    max_links_per_email: int = 3
    link_timeout_seconds: int = 10
    max_download_bytes: int = 5_000_000

    skip_bulk_mail: bool = False
    never_skip_senders: list[str] = field(default_factory=list)

    read_pdfs: bool = True
    max_pdf_pages: int = 30

    safe_mode: bool = True
    enable_refusal_fallbacks: bool = False

    output: str = "csv"                # "csv" | "sheets"
    csv_path: str = "data/results.csv"
    spreadsheet_id: str = ""
    sheet_name: str = "Тріаж"

    state_db: str = "data/state.db"
    error_log: str = "data/errors.jsonl"
    credentials_file: str = "secrets/credentials.json"
    token_file: str = "secrets/token.json"

    raw: dict = field(default_factory=dict)

    @property
    def scopes(self) -> list[str]:
        scopes = [GMAIL_READONLY_SCOPE]
        if self.output == "sheets":
            scopes.append(SHEETS_SCOPE)
        return scopes

    def validate(self) -> None:
        if not self.business_context.strip():
            raise ConfigError(
                "У config.yaml порожній business_context. Опишіть своїми словами, "
                "що для вашого бізнесу важливо, а що ні — без цього класифікація буде навмання."
            )
        if self.output not in ("csv", "sheets"):
            raise ConfigError(f"output має бути 'csv' або 'sheets', а не {self.output!r}.")
        if self.output == "sheets" and not self.spreadsheet_id:
            raise ConfigError(
                "output: sheets, але spreadsheet_id порожній. "
                "Вставте ID таблиці з її адреси між /d/ і /edit."
            )
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise ConfigError("confidence_threshold має бути між 0 і 1.")
        if self.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigError(
                "effort має бути одним із: low, medium, high, xhigh, max."
            )
        if not self.safe_mode:
            raise ConfigError(
                "safe_mode: false заблоковано. Ця версія системи вміє лише читати "
                "й записувати звіт; режим із діями ще не існує."
            )

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        p = Path(path)
        if not p.exists():
            raise ConfigError(
                f"Немає файлу {p}. Скопіюйте config.example.yaml у config.yaml "
                f"і заповніть його."
            )
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        known = {f for f in cls.__dataclass_fields__ if f != "raw"}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(
                f"У config.yaml є невідомі поля: {', '.join(sorted(unknown))}. "
                f"Перевірте написання за config.example.yaml."
            )
        cfg = cls(**{k: v for k, v in data.items() if k in known}, raw=data)
        cfg.validate()
        return cfg


def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise ConfigError(
            "Не заданий ANTHROPIC_API_KEY. Покладіть його у файл .env поряд із run.py "
            "у вигляді: ANTHROPIC_API_KEY=sk-ant-..."
        )
    return key


def load_dotenv(path: str | Path = ".env") -> None:
    """Мінімальний .env-рідер, щоб не тягнути зайву залежність."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
