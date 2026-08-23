"""Пошук посилань у тексті листа."""
from __future__ import annotations

import re
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]}]+", re.IGNORECASE)

# Домени, за якими нема сенсу ходити: трекери відписок, кліки розсилок тощо.
_SKIP_HOST_PARTS = (
    "unsubscribe", "list-manage", "mailchimp", "sendgrid.net", "sparkpostmail",
    "doubleclick", "googleadservices", "mailtrack", "click.", "track.", "email.",
)


def _is_useful(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return not any(part in host for part in _SKIP_HOST_PARTS)


def find_links(text: str, limit: int = 3) -> list[str]:
    """Повертає до `limit` унікальних змістовних посилань у порядку появи."""
    seen: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:!?")
        if url in seen or not _is_useful(url):
            continue
        seen.append(url)
        if len(seen) >= limit:
            break
    return seen
