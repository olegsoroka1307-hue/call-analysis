"""Отримання тексту зі сторінок, на які веде посилання в листі.

Посилання в пошті контролює відправник, тому запит навмисно параноїдальний:
тільки http/https, ніяких приватних адрес, обмеження за часом і розміром.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ..models import ExtractedDoc

_UA = "Mozilla/5.0 (compatible; EmailTriageBot/1.0; +read-only)"
_DROP_TAGS = ("script", "style", "noscript", "svg", "nav", "footer", "form", "header")


def _resolves_to_private(hostname: str) -> bool:
    """True, якщо хост вказує на внутрішню мережу (захист від SSRF)."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError):
        return True  # не резолвиться — не ходимо
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return True
    return False


def fetch_page_text(
    url: str,
    *,
    timeout: int = 10,
    max_bytes: int = 5_000_000,
    max_chars: int = 8000,
    session: requests.Session | None = None,
) -> ExtractedDoc:
    """Завантажує сторінку й повертає її видимий текст. Виняток не кидає."""
    doc = ExtractedDoc(source=url, kind="web", text="")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        doc.error = f"схема {parsed.scheme!r} не дозволена"
        return doc
    if not parsed.hostname:
        doc.error = "в посиланні немає хоста"
        return doc
    if _resolves_to_private(parsed.hostname):
        doc.error = "посилання веде у внутрішню мережу — пропущено з міркувань безпеки"
        return doc

    http = session or requests.Session()
    try:
        response = http.get(
            url,
            timeout=timeout,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
            stream=True,
            allow_redirects=True,
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type and "text" not in content_type:
            doc.error = f"тип вмісту {content_type or 'невідомий'} — не сторінка"
            return doc

        chunks, total = [], 0
        for chunk in response.iter_content(65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                doc.truncated = True
                break
        raw = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    except requests.RequestException as exc:
        doc.error = f"не вдалося відкрити сторінку: {type(exc).__name__}: {exc}"
        return doc
    finally:
        if session is None:
            http.close()

    try:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(list(_DROP_TAGS)):
            tag.decompose()
        title = soup.title.get_text(strip=True) if soup.title else ""
        body = soup.get_text(separator="\n", strip=True)
        text = "\n".join(line for line in body.splitlines() if line.strip())
        if title:
            text = f"{title}\n\n{text}"
        if len(text) > max_chars:
            text = text[:max_chars]
            doc.truncated = True
        doc.text = text
        if not text.strip():
            doc.error = "сторінка порожня або будується скриптом"
    except Exception as exc:
        doc.error = f"не вдалося розібрати HTML: {type(exc).__name__}: {exc}"
    return doc
