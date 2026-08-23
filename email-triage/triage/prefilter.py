"""Дешевий детермінований фільтр масової пошти.

Навіщо. Кожен лист, що йде в модель, коштує грошей. У типовій скриньці
більшість вхідного — розсилки, які машина впізнає за службовим заголовком
List-Unsubscribe без жодного ШІ. Пропускати їх повз модель — найбільший
важіль вартості в усій системі.

Ризик. Розсилка теж буває важливою: клієнт може писати з сервісу email-маркетингу.
Тому фільтр вимкнений за замовчуванням, правила мінімальні, а кожен відсіяний
лист усе одно потрапляє у звіт із позначкою — щоб рішення можна було перевірити.
"""
from __future__ import annotations

from .models import EmailMessage, Verdict

BULK_HEADERS = ("list-unsubscribe", "list-id", "precedence")
BULK_PRECEDENCE = ("bulk", "list", "junk")


def looks_like_bulk(email: EmailMessage) -> str | None:
    """Повертає назву ознаки масової розсилки або None."""
    headers = {k.lower(): v for k, v in (email.headers or {}).items()}
    if "list-unsubscribe" in headers:
        return "List-Unsubscribe"
    if "list-id" in headers:
        return "List-Id"
    precedence = headers.get("precedence", "").strip().lower()
    if precedence in BULK_PRECEDENCE:
        return f"Precedence: {precedence}"
    return None


def prefilter(email: EmailMessage, never_skip: list[str]) -> Verdict | None:
    """NOT_IMPORTANT без звернення до моделі — або None, якщо треба думати.

    Умови навмисно вузькі: будь-який сумнів → лист іде звичайним шляхом.
    """
    marker = looks_like_bulk(email)
    if not marker:
        return None
    if email.attachments:
        return None  # у розсилок не буває важливих вкладень; якщо є — хай дивиться модель
    sender = (email.sender or "").lower()
    if any(needle.lower() in sender for needle in never_skip if needle.strip()):
        return None
    return Verdict(
        label="NOT_IMPORTANT",
        confidence=1.0,
        reason=(
            f"Масова розсилка за заголовком {marker}, без вкладень. "
            "Відсіяно до звернення до моделі."
        ),
        sender_summary=email.sender,
        topic=email.subject,
        degraded=True,
    )
