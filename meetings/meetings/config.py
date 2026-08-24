"""Завантаження та перевірка конфігурації.

Власник редагує лише config.yaml простою мовою. Три секрети — ключ Anthropic,
токен бота й ключ інтеграції Notion — живуть в .env і в конфіг не потрапляють.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import MEDIUM, PRIORITIES

# Версія Notion API. Notion вимагає її в кожному запиті й ламає сумісність
# без неї, тому дата зафіксована тут, а не береться «найсвіжіша».
NOTION_VERSION = "2022-06-28"


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    team_context: str

    notion_database_id: str = ""
    send_telegram: bool = True

    model: str = "claude-opus-5"
    effort: str = "high"
    output_language: str = "українська"
    max_transcript_chars: int = 120_000
    max_output_tokens: int = 16_000

    # Скільки задач максимум узяти з однієї наради. Захист від того, що модель
    # нафантазує сто пунктів із півгодинної розмови й завалить ними людей.
    max_commitments: int = 30

    default_priority: str = MEDIUM
    ask_before_guessing_deadline: bool = False

    registry_db: str = "data/registry.db"
    error_log: str = "data/errors.jsonl"

    request_timeout_seconds: int = 30
    safe_mode: bool = True

    raw: dict = field(default_factory=dict)

    def validate(self) -> None:
        if not self.team_context.strip():
            raise ConfigError(
                "У config.yaml порожній team_context. Опишіть своїми словами, хто у вас "
                "у команді й хто за що відповідає — без цього модель не зрозуміє, "
                "кому призначати задачі."
            )
        if self.default_priority not in PRIORITIES:
            raise ConfigError(
                f"default_priority має бути одним із: {', '.join(PRIORITIES)}."
            )
        if self.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigError("effort має бути одним із: low, medium, high, xhigh, max.")
        if self.max_transcript_chars < 1000:
            raise ConfigError("max_transcript_chars менший за 1000 — це не транскрипція.")
        if self.max_commitments < 1:
            raise ConfigError("max_commitments має бути щонайменше 1.")
        if not self.safe_mode:
            raise ConfigError(
                "safe_mode: false заблоковано. Ця версія вміє лише створювати задачі "
                "й міняти їм статус; видаляти вона не вміє взагалі."
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


def _secret(name: str, hint: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Не заданий {name}. {hint}")
    return value


def api_key() -> str:
    return _secret(
        "ANTHROPIC_API_KEY",
        "Покладіть його у файл .env поряд із run.py: ANTHROPIC_API_KEY=sk-ant-...",
    )


def notion_key() -> str:
    return _secret(
        "NOTION_API_KEY",
        "Створіть інтеграцію на notion.so/my-integrations і додайте в .env: "
        "NOTION_API_KEY=secret_... Не забудьте поділитися базою з інтеграцією.",
    )


def telegram_token() -> str:
    return _secret(
        "TELEGRAM_BOT_TOKEN",
        "Візьміть у @BotFather (/newbot) і додайте в .env: "
        "TELEGRAM_BOT_TOKEN=1234567:AA...",
    )


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
