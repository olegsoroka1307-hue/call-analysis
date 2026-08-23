"""Клієнт Freelancehunt: правильний запит і розбір відповіді."""
from __future__ import annotations

import pytest

from monitor.freelancehunt import (
    BASE_URL, AuthError, FreelancehuntClient, FreelancehuntError, RateLimited,
    to_project,
)

from .fakes import FakeHttpResponse, FakeSession, fh_item


class TestRequestShape:
    def test_sends_bearer_token_and_language(self):
        session = FakeSession([[]])
        FreelancehuntClient("tok-123", language="uk", session=session).projects()
        call = session.calls[0]
        assert call["url"] == BASE_URL + "/projects"
        assert call["headers"]["Authorization"] == "Bearer tok-123"
        assert call["headers"]["Accept-Language"] == "uk"

    def test_unsupported_language_is_omitted(self):
        session = FakeSession([[]])
        client = FreelancehuntClient("t", language="pl", session=session)
        assert "Accept-Language" not in client.headers

    def test_paginates_and_stops_on_empty_page(self):
        session = FakeSession([[fh_item(1, "a", "b")], []])
        projects = FreelancehuntClient("t", session=session).projects(pages=5)
        assert len(projects) == 1
        assert len(session.calls) == 2  # третю сторінку вже не питали
        assert session.calls[1]["params"]["page[number]"] == 2

    def test_skill_filter_is_comma_joined(self):
        session = FakeSession([[]])
        FreelancehuntClient("t", session=session).projects(skill_ids=[56, 92])
        assert session.calls[0]["params"]["filter[skill_id]"] == "56,92"


class TestErrors:
    def test_401_explains_where_to_get_the_token(self):
        session = FakeSession([FakeHttpResponse({}, status=401)])
        with pytest.raises(AuthError, match="Додатки та API"):
            FreelancehuntClient("bad", session=session).projects()

    def test_429_is_its_own_error(self):
        session = FakeSession([FakeHttpResponse({}, status=429)])
        with pytest.raises(RateLimited):
            FreelancehuntClient("t", session=session).projects()

    def test_500_is_reported(self):
        session = FakeSession([FakeHttpResponse({}, status=500)])
        with pytest.raises(FreelancehuntError, match="500"):
            FreelancehuntClient("t", session=session).projects()

    def test_connection_failure_is_wrapped(self):
        import requests

        session = FakeSession([requests.ConnectionError("no route")])
        with pytest.raises(FreelancehuntError, match="зʼєднатися"):
            FreelancehuntClient("t", session=session).projects()

    def test_non_json_is_reported(self):
        session = FakeSession([FakeHttpResponse(ValueError("not json"))])
        with pytest.raises(FreelancehuntError, match="не-JSON"):
            FreelancehuntClient("t", session=session).projects()


class TestParsing:
    def test_maps_all_fields(self):
        project = to_project(fh_item(
            299165, "Потрібен бот для задач",
            "Telegram + Notion, автоматизація",
            skills=["Python", "Боти"], amount=15000, bid_count=4,
        ))
        assert project.project_id == "299165"
        assert project.title == "Потрібен бот для задач"
        assert project.skills == ["Python", "Боти"]
        assert project.budget_amount == 15000
        assert project.budget_currency == "UAH"
        assert project.bid_count == 4
        assert project.url.endswith("299165.html")
        assert project.employer == "Олег К."
        assert project.published_at.year == 2026

    def test_missing_budget_is_none_not_zero(self):
        project = to_project(fh_item(1, "a", "b", amount=None))
        assert project.budget_amount is None
        assert project.budget_text == "бюджет не вказано"

    def test_budget_is_formatted_readably(self):
        assert to_project(fh_item(1, "a", "b", amount=15000)).budget_text == "15 000 UAH"

    def test_survives_a_sparse_record(self):
        project = to_project({"id": 7, "attributes": {}, "links": {}})
        assert project.project_id == "7"
        assert project.title == "(без назви)"
        assert project.url.endswith("/7")

    def test_rate_limit_header_is_captured(self):
        session = FakeSession([
            FakeHttpResponse({"data": []}, headers={"X-Ratelimit-Remaining": "88"})
        ])
        client = FreelancehuntClient("t", session=session)
        client.projects()
        assert client.rate_limit_remaining == "88"
