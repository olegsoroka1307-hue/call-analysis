from __future__ import annotations

import pytest

from meetings.models import Commitment
from meetings.notion import NotionClient, NotionError
from .fakes import FakeResponse, FakeSession


def client(responses=None) -> tuple[NotionClient, FakeSession]:
    session = FakeSession(responses)
    return NotionClient("secret_x", "db-1", session=session), session


def commitment(**kwargs) -> Commitment:
    base = dict(
        responsible="Саша", task="Надіслати КП", deadline="2026-08-28",
        priority="Высокий", project="Продажі", quote="Скину до четверга.",
        source="Планірка — 2026-08-24",
    )
    base.update(kwargs)
    return Commitment(**base)


def test_create_task_sends_every_field(cfg):
    notion, session = client([FakeResponse(200, {"id": "page-1"})])
    assert notion.create_task(commitment()) == "page-1"

    props = session.requests[0]["json"]["properties"]
    assert session.requests[0]["json"]["parent"] == {"database_id": "db-1"}
    assert props["Задача"]["title"][0]["text"]["content"] == "Надіслати КП"
    assert props["Ответственный"]["rich_text"][0]["text"]["content"] == "Саша"
    assert props["Дедлайн"]["date"]["start"] == "2026-08-28"
    assert props["Приоритет"]["select"]["name"] == "Высокий"
    assert props["Статус"]["select"]["name"] == "Не начата"
    assert props["Проект"]["multi_select"] == [{"name": "Продажі"}]
    assert props["Цитата"]["rich_text"][0]["text"]["content"] == "Скину до четверга."


def test_task_without_deadline_omits_the_field():
    # Notion відхиляє date: {"start": ""} помилкою валідації.
    notion, session = client([FakeResponse(200, {"id": "page-1"})])
    notion.create_task(commitment(deadline="", project=""))
    props = session.requests[0]["json"]["properties"]
    assert "Дедлайн" not in props
    assert "Проект" not in props


def test_very_long_quote_is_trimmed_to_the_notion_limit():
    notion, session = client([FakeResponse(200, {"id": "page-1"})])
    notion.create_task(commitment(quote="я" * 5000))
    text = session.requests[0]["json"]["properties"]["Цитата"]["rich_text"][0]["text"]["content"]
    assert len(text) == 2000
    assert text.endswith("…")


def test_missing_page_id_is_an_error():
    notion, _ = client([FakeResponse(200, {})])
    with pytest.raises(NotionError, match="не повернув id"):
        notion.create_task(commitment())


def test_set_status_refuses_an_unknown_status():
    notion, session = client()
    with pytest.raises(NotionError, match="невідомий статус"):
        notion.set_status("page-1", "Почти готово")
    assert session.requests == []


def test_set_status_patches_the_page():
    notion, session = client([FakeResponse(200, {"id": "page-1"})])
    notion.set_status("page-1", "Готово")
    assert session.requests[0]["method"] == "PATCH"
    assert session.requests[0]["url"].endswith("/pages/page-1")
    assert session.requests[0]["json"]["properties"]["Статус"]["select"]["name"] == "Готово"


def test_query_reads_every_page_of_results():
    first = FakeResponse(200, {
        "results": [{
            "id": "p1",
            "properties": {
                "Задача": {"type": "title", "title": [{"plain_text": "КП"}]},
                "Ответственный": {"type": "rich_text", "rich_text": [{"plain_text": "Саша"}]},
                "Статус": {"type": "select", "select": {"name": "Готово"}},
                "Дедлайн": {"type": "date", "date": {"start": "2026-08-28"}},
                "Проект": {"type": "multi_select", "multi_select": [{"name": "Продажі"}]},
            },
        }],
        "has_more": True,
        "next_cursor": "cur-2",
    })
    second = FakeResponse(200, {"results": [{"id": "p2", "properties": {}}], "has_more": False})
    notion, session = client([first, second])

    rows = notion.query_tasks()
    assert [r["page_id"] for r in rows] == ["p1", "p2"]
    assert rows[0] == {
        "page_id": "p1", "task": "КП", "responsible": "Саша", "status": "Готово",
        "deadline": "2026-08-28", "priority": "", "project": "Продажі", "source": "",
    }
    assert rows[1]["status"] == "Не начата"      # порожня сторінка не ламає звіт
    assert session.requests[1]["json"]["start_cursor"] == "cur-2"


def test_query_by_meeting_adds_a_filter():
    notion, session = client([FakeResponse(200, {"results": [], "has_more": False})])
    notion.query_tasks("Планірка")
    assert session.requests[0]["json"]["filter"] == {
        "property": "Источник встречи", "rich_text": {"contains": "Планірка"}
    }


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (401, {"message": "unauthorized"}, "NOTION_API_KEY"),
        (404, {"message": "not found", "code": "object_not_found"}, "поділилися з інтеграцією"),
        (429, {"message": "rate limited"}, "зачекати"),
        (400, {"message": "bad", "code": "validation_error"}, "звірте назви полів"),
        (503, {"message": "down"}, "503"),
    ],
)
def test_errors_explain_what_to_do(status, body, expected):
    notion, _ = client([FakeResponse(status, body, headers={"Retry-After": "3"})])
    with pytest.raises(NotionError, match=expected):
        notion.create_task(commitment())


def test_network_failure_is_wrapped():
    import requests

    notion, _ = client([requests.ConnectionError("немає мережі")])
    with pytest.raises(NotionError, match="зʼєднатися з Notion"):
        notion.create_task(commitment())


def test_create_database_defines_all_fields():
    notion, session = client([FakeResponse(200, {"id": "db-new"})])
    assert notion.create_database("page-parent") == "db-new"
    props = session.requests[0]["json"]["properties"]
    assert set(props) == {
        "Задача", "Ответственный", "Дедлайн", "Приоритет",
        "Статус", "Проект", "Источник встречи", "Цитата",
    }
    statuses = [o["name"] for o in props["Статус"]["select"]["options"]]
    assert statuses == ["Не начата", "В работе", "Готово", "Отложено", "Отменена"]
    assert notion.database_id == "db-new"
