"""Тонкий клієнт Freelancehunt API 2.0.

Контракт узятий із офіційної клієнтської бібліотеки `freelancehunt-api`
(PyPI), бо сайт документації недоступний із середовища розробки:

    GET https://api.freelancehunt.com/v2/projects
    Authorization: Bearer <token>
    Accept-Language: uk | ru | en
    ?filter[skill_id]=1,2  &  page[number]=1

Відповідь у форматі JSON:API — {"data": [{"id", "attributes", "links"}]}.

Клієнт уміє тільки читати. Подавати відгуки автоматично він не вміє
і не повинен: масові автовідгуки псують репутацію на біржі швидше,
ніж приносять замовлення.
"""
from __future__ import annotations

from datetime import datetime

import requests

from .models import Project

BASE_URL = "https://api.freelancehunt.com/v2"


class FreelancehuntError(RuntimeError):
    pass


class AuthError(FreelancehuntError):
    pass


class RateLimited(FreelancehuntError):
    pass


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def to_project(item: dict) -> Project:
    """Перетворює один запис JSON:API на нашу структуру."""
    attrs = item.get("attributes") or {}
    budget = attrs.get("budget") or {}
    employer = attrs.get("employer") or {}
    links = (item.get("links") or {}).get("self") or {}
    amount = budget.get("amount")
    return Project(
        source="freelancehunt",
        project_id=str(item.get("id", "")),
        title=attrs.get("name") or "(без назви)",
        description=attrs.get("description") or "",
        url=links.get("web") or f"https://freelancehunt.com/project/{item.get('id')}",
        skills=[s.get("name", "") for s in (attrs.get("skills") or []) if s.get("name")],
        budget_amount=float(amount) if amount is not None else None,
        budget_currency=budget.get("currency") or "",
        bid_count=int(attrs.get("bid_count") or 0),
        employer=" ".join(
            x for x in (employer.get("first_name"), employer.get("last_name")) if x
        ) or employer.get("login", ""),
        published_at=_parse_dt(attrs.get("published_at")),
        is_only_for_plus=bool(attrs.get("is_only_for_plus")),
    )


class FreelancehuntClient:
    def __init__(
        self,
        token: str,
        *,
        language: str = "uk",
        session: requests.Session | None = None,
        timeout: int = 20,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self.session = session or requests.Session()
        self.headers = {"Authorization": f"Bearer {token}"}
        if language in ("uk", "ru", "en"):
            self.headers["Accept-Language"] = language
        self.rate_limit_remaining: str | None = None

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            response = self.session.get(
                BASE_URL + path,
                headers=self.headers,
                params=params or {},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise FreelancehuntError(
                f"не вдалося зʼєднатися з Freelancehunt: {type(exc).__name__}: {exc}"
            ) from exc

        self.rate_limit_remaining = response.headers.get("X-Ratelimit-Remaining")

        if response.status_code == 401:
            raise AuthError(
                "Freelancehunt не прийняв токен. Перевірте FREELANCEHUNT_TOKEN — "
                "його видно в акаунті, розділ «Додатки та API»."
            )
        if response.status_code == 429:
            raise RateLimited("перевищено ліміт запитів до Freelancehunt")
        if response.status_code >= 400:
            raise FreelancehuntError(
                f"Freelancehunt повернув помилку {response.status_code}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise FreelancehuntError("Freelancehunt повернув не-JSON") from exc

    def projects(self, *, pages: int = 1, skill_ids: list[int] | None = None) -> list[Project]:
        """Відкриті замовлення, від найновіших."""
        collected: list[Project] = []
        for page in range(1, pages + 1):
            params: dict = {"page[number]": page}
            if skill_ids:
                params["filter[skill_id]"] = ",".join(str(s) for s in skill_ids)
            payload = self._get("/projects", params)
            items = payload.get("data") or []
            if not items:
                break
            collected.extend(to_project(item) for item in items)
        return collected

    def skills(self) -> list[dict]:
        """Довідник навичок — потрібен, щоб звузити пошук фільтром."""
        return self._get("/skills").get("data") or []
