"""Налаштування монітора: config.yaml простою мовою + секрети з .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(RuntimeError):
    pass


DEFAULT_KEYWORDS: dict = {
    "meetings": {
        "weight": 5,
        "words": [
            "зустріч", "встреч", "meeting", "дзвінок", "звонок", "call",
            "транскрип", "transcri", "протокол", "нотатк", "заметк",
            "action item", "мінутки", "планірк", "планерк",
        ],
    },
    "email": {
        "weight": 5,
        "words": [
            "пошт", "почт", "email", "e-mail", "gmail", "inbox", "вхідн",
            "розсилк", "рассылк", "листуван", "переписк",
        ],
    },
    "tools": {
        "weight": 4,
        "words": [
            "notion", "telegram", "телеграм", "бот", "bot", "crm",
            "gpt", "chatgpt", "claude", "ші ", "штучний інтелект",
            "искусственный интеллект", " ai ", "airtable", "google sheets",
        ],
    },
    "automation": {
        "weight": 3,
        "words": [
            "автоматиз", "automation", "n8n", "make.com", "integromat",
            "zapier", "інтеграц", "интеграц", "webhook", "вебхук", "api",
            "парсинг", "скрипт",
        ],
    },
    # Люди, які ще не знають, що їм потрібна автоматизація. Вони не пишуть
    # «потрібен n8n» — вони описують ручну роботу: «щодня зводимо Excel від
    # пʼяти постачальників». Саме тут найменше конкурентів, тому вага така
    # сама, як у наших профільних груп.
    "process": {
        "weight": 5,
        "words": [
            # сама ознака ручної роботи
            "вручну", "вручную", "руками", "manual", "by hand",
            "рутин", "routine", "повторюван", "повторяющ", "repetitive",
            "копіпаст", "copy paste", "copy-paste", "копіюва", "копирова",
            "щодня", "щоденно", "кожен день", "ежедневно", "каждый день",
            # робота з даними й документами
            "excel", "spreadsheet", "таблиц", "csv",
            "внесення даних", "внесение данных", "data entry",
            "зводити", "зведення", "сводить", "сведение", "consolidat",
            "звірка", "сверка", "reconcil",
            "заповню", "заполня", "заносити", "заносить",
            "накладн", "рахунк-фактур", "invoice", "інвойс", "инвойс",
            "обробк замовлен", "обработк заказ", "order processing",
            "pdf", "витягувати дані", "извлекать данные", "extract data",
        ],
    },
    "tasks": {
        "weight": 2,
        "words": [
            "задач", "трекер", "tracker", "дедлайн", "нагадуван",
            "напоминан", "звіт", "отчет", "report", "контроль виконан",
        ],
    },
}

DEFAULT_STOP_WORDS: list[str] = [
    "логотип", "банер", "баннер", "копірайт", "копирайт", "рерайт",
    "seo-текст", "верстк", "переклад тексту", "перевод текста",
    "набір тексту", "набор текста", "фотошоп", "відеомонтаж", "видеомонтаж",
]


@dataclass
class Config:
    # Що шукаємо
    keywords: dict = field(default_factory=lambda: DEFAULT_KEYWORDS.copy())
    stop_words: list[str] = field(default_factory=lambda: list(DEFAULT_STOP_WORDS))
    min_score: int = 6
    min_budget: dict = field(
        default_factory=lambda: {"UAH": 3000, "USD": 80, "EUR": 80}
    )
    skip_plus_only: bool = True
    max_bids: int = 25
    pages: int = 2
    language: str = "uk"

    # Чернетка відгуку
    draft_replies: bool = True
    draft_min_score: int = 10
    model: str = "claude-opus-5"
    effort: str = "medium"
    my_offer: str = ""

    # Сповіщення
    notify_telegram: bool = False
    telegram_chat_id: str = ""
    csv_path: str = "data/leads.csv"
    state_db: str = "data/state.db"
    error_log: str = "data/errors.jsonl"

    raw: dict = field(default_factory=dict)

    def validate(self) -> None:
        if self.min_score < 1:
            raise ConfigError("min_score має бути додатним числом.")
        if self.pages < 1 or self.pages > 10:
            raise ConfigError("pages має бути від 1 до 10.")
        if self.draft_replies and not self.my_offer.strip():
            raise ConfigError(
                "draft_replies: true, але порожній my_offer. Опишіть свою послугу "
                "своїми словами — без цього чернетка відгуку буде беззмістовною."
            )
        if self.notify_telegram and not self.telegram_chat_id.strip():
            raise ConfigError(
                "notify_telegram: true, але немає telegram_chat_id. "
                "Як його дізнатись — у README, розділ «Сповіщення в Telegram»."
            )
        for group, spec in self.keywords.items():
            if not isinstance(spec, dict) or "words" not in spec:
                raise ConfigError(
                    f"Група ключових слів {group!r} має містити список words."
                )

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        p = Path(path)
        if not p.exists():
            raise ConfigError(
                f"Немає файлу {p}. Скопіюйте config.example.yaml у config.yaml."
            )
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        known = {f for f in cls.__dataclass_fields__ if f != "raw"}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(
                f"Невідомі поля в config.yaml: {', '.join(sorted(unknown))}."
            )
        cfg = cls(**{k: v for k, v in data.items() if k in known}, raw=data)
        cfg.validate()
        return cfg


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def require(name: str, hint: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Не заданий {name}. {hint}")
    return value
