from __future__ import annotations

import pytest

from meetings.models import Commitment
from meetings.notion import NotionClient, NotionError, clean_option
from .fakes import FakeResponse, FakeSession, db_schema

PAGE_OK = FakeResponse(200, {"id": "page-1"})


def client(responses=None) -> tuple[NotionClient, FakeSession]:
    session = FakeSession(responses)
    return NotionClient("secret_x", "db-1", session=session), session


def commitment(**kwargs) -> Commitment:
    base = dict(
        responsible="Саша", task="Надіслати КП", deadline="2026-08-28",
        priority="Високий", project="Продажі", quote="Скину до четверга.",
        source="Планірка — 2026-08-24",
    )
    base.update(kwargs)
    return Commitment(**base)


def posted(session: FakeSession) -> dict:
    """Тіло запиту, яким створювалася сторінка."""
    return next(r for r in session.requests if r["url"].endswith("/pages"))["json"]


def test_create_task_sends_every_field():
    # Опція «Продажі» в базі вже є, тому схему читаємо й одразу пишемо.
    notion, session = client([db_schema(["Продажі"]), PAGE_OK])
    assert notion.create_task(commitment()) == "page-1"

    body = posted(session)
    props = body["properties"]
    assert body["parent"] == {"database_id": "db-1"}
    assert props["Задача"]["title"][0]["text"]["content"] == "Надіслати КП"
    assert props["Відповідальний"]["rich_text"][0]["text"]["content"] == "Саша"
    assert props["Дедлайн"]["date"]["start"] == "2026-08-28"
    assert props["Пріоритет"]["select"]["name"] == "Високий"
    assert props["Статус"]["select"]["name"] == "Не почато"
    assert props["Проєкт"]["multi_select"] == [{"name": "Продажі"}]
    assert props["Джерело"]["rich_text"][0]["text"]["content"] == "Планірка — 2026-08-24"
    assert props["Цитата"]["rich_text"][0]["text"]["content"] == "Скину до четверга."
    assert notion.notes == []


def test_task_without_deadline_omits_the_field():
    # Notion відхиляє date: {"start": ""} помилкою валідації.
    notion, session = client([PAGE_OK])
    notion.create_task(commitment(deadline="", project=""))
    props = posted(session)["properties"]
    assert "Дедлайн" not in props
    assert "Проєкт" not in props


def test_very_long_quote_is_trimmed_to_the_notion_limit():
    notion, session = client([PAGE_OK])
    notion.create_task(commitment(quote="я" * 5000, project=""))
    text = posted(session)["properties"]["Цитата"]["rich_text"][0]["text"]["content"]
    assert len(text) == 2000
    assert text.endswith("…")


def test_missing_page_id_is_an_error():
    notion, _ = client([FakeResponse(200, {})])
    with pytest.raises(NotionError, match="не повернув id"):
        notion.create_task(commitment(project=""))


# ── опції поля «Проєкт» ─────────────────────────────────────────────
# Notion відхиляє запис із опцією, якої немає в базі. Модель називає проєкт
# із контексту наради, тож нова назва — питання часу, а не виняток.

def test_new_project_option_is_created_before_the_task():
    notion, session = client([
        db_schema(["Будівництво"]),          # GET схеми: «Продажів» ще немає
        FakeResponse(200, {"id": "db-1"}),   # PATCH: завели опцію
        PAGE_OK,                             # POST: сторінка
    ])
    assert notion.create_task(commitment()) == "page-1"

    methods = [(r["method"], r["url"].rsplit("/v1", 1)[1]) for r in session.requests]
    assert methods == [
        ("GET", "/databases/db-1"),
        ("PATCH", "/databases/db-1"),
        ("POST", "/pages"),
    ]
    # У PATCH ідуть і старі опції: Notion замінює список цілком, і без них
    # проєкти на вже створених задачах були б стерті.
    options = session.requests[1]["json"]["properties"]["Проєкт"]["multi_select"]["options"]
    assert options == [{"name": "Будівництво"}, {"name": "Продажі"}]
    assert posted(session)["properties"]["Проєкт"]["multi_select"] == [{"name": "Продажі"}]


def test_task_survives_when_the_option_cannot_be_created():
    # Головне: задача все одно записується. Втратити її через назву проєкту
    # не можна — саме так це падало у клієнта.
    notion, session = client([
        db_schema([]),
        FakeResponse(403, {"message": "no update capability"}),
        PAGE_OK,
    ])
    assert notion.create_task(commitment()) == "page-1"

    assert "Проєкт" not in posted(session)["properties"]
    notes = notion.take_notes()
    assert any("не вдалося додати опцію «Продажі»" in note for note in notes)
    assert any("записано без поля «Проєкт»" in note for note in notes)
    assert notion.take_notes() == []          # забрані зауваження не дублюються


def test_a_hopeless_option_is_attempted_only_once():
    # Права Update не видані. Нарада — це десяток задач з тим самим проєктом:
    # без памʼяті про невдачу кожна повторювала б той самий марний PATCH.
    notion, session = client([
        db_schema([]),
        FakeResponse(403, {"message": "no update capability"}),
        PAGE_OK, PAGE_OK, PAGE_OK,
    ])
    for _ in range(3):
        notion.create_task(commitment())

    assert [r["method"] for r in session.requests].count("PATCH") == 1
    # Але кожна задача окремо каже, що пішла без проєкту.
    assert sum("записано без поля" in note for note in notion.notes) == 3
    assert sum("не вдалося додати опцію" in note for note in notion.notes) == 1


def test_unreadable_schema_does_not_lose_the_task():
    notion, session = client([FakeResponse(500, {"message": "down"}), PAGE_OK])
    assert notion.create_task(commitment()) == "page-1"
    assert "Проєкт" not in posted(session)["properties"]
    assert any("прочитати опції" in note for note in notion.notes)


def test_schema_is_read_once_for_the_whole_meeting():
    # Нарада — це десяток задач підряд. Читати схему щоразу означало б
    # десяток зайвих запитів і ризик упертися в ліміт Notion.
    notion, session = client([db_schema(["Продажі"]), PAGE_OK, PAGE_OK, PAGE_OK])
    for _ in range(3):
        notion.create_task(commitment())
    assert [r["method"] for r in session.requests].count("GET") == 1


def test_option_created_once_is_reused():
    notion, session = client([
        db_schema([]), FakeResponse(200, {"id": "db-1"}), PAGE_OK, PAGE_OK,
    ])
    notion.create_task(commitment())
    notion.create_task(commitment())
    assert [r["method"] for r in session.requests].count("PATCH") == 1


def test_comma_in_the_project_name_is_replaced():
    # Notion забороняє кому в назві опції — з нею впав би весь запис.
    assert clean_option("Продажі, Маркетинг") == "Продажі / Маркетинг"
    notion, session = client([db_schema([]), FakeResponse(200, {"id": "db-1"}), PAGE_OK])
    notion.create_task(commitment(project="Продажі, Маркетинг"))
    assert posted(session)["properties"]["Проєкт"]["multi_select"] == [
        {"name": "Продажі / Маркетинг"}
    ]


def test_unknown_priority_falls_back_instead_of_failing():
    # Набір пріоритетів наш, тому звіряємо на своєму боці, а не ловимо 400.
    notion, session = client([PAGE_OK])
    notion.create_task(commitment(priority="Дуже терміновий", project=""))
    assert posted(session)["properties"]["Пріоритет"]["select"]["name"] == "Середній"
    assert any("Дуже терміновий" in note for note in notion.notes)


# ── статуси й читання ───────────────────────────────────────────────
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
                "Відповідальний": {"type": "rich_text", "rich_text": [{"plain_text": "Саша"}]},
                "Статус": {"type": "select", "select": {"name": "Готово"}},
                "Дедлайн": {"type": "date", "date": {"start": "2026-08-28"}},
                "Проєкт": {"type": "multi_select", "multi_select": [{"name": "Продажі"}]},
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
    assert rows[1]["status"] == "Не почато"      # порожня сторінка не ламає звіт
    assert session.requests[1]["json"]["start_cursor"] == "cur-2"


def test_query_by_meeting_adds_a_filter():
    notion, session = client([FakeResponse(200, {"results": [], "has_more": False})])
    notion.query_tasks("Планірка")
    assert session.requests[0]["json"]["filter"] == {
        "property": "Джерело", "rich_text": {"contains": "Планірка"}
    }


# ── помилки ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "status, body, expected",
    [
        (401, {"message": "unauthorized"}, "NOTION_API_KEY"),
        (404, {"message": "not found", "code": "object_not_found"}, "доступ інтеграції"),
        (429, {"message": "rate limited"}, "зачекати"),
        (400, {"message": "bad", "code": "validation_error"}, "звірте назви полів"),
        (503, {"message": "down"}, "503"),
    ],
)
def test_errors_explain_what_to_do(status, body, expected):
    notion, _ = client([FakeResponse(status, body, headers={"Retry-After": "3"})])
    with pytest.raises(NotionError, match=expected):
        notion.create_task(commitment(project=""))


def test_network_failure_is_wrapped():
    import requests

    notion, _ = client([requests.ConnectionError("немає мережі")])
    with pytest.raises(NotionError, match="зʼєднатися з Notion"):
        notion.create_task(commitment(project=""))


def test_create_database_defines_all_fields():
    notion, session = client([FakeResponse(200, {"id": "db-new"})])
    assert notion.create_database("page-parent") == "db-new"
    props = session.requests[0]["json"]["properties"]
    assert set(props) == {
        "Задача", "Відповідальний", "Дедлайн", "Пріоритет",
        "Статус", "Проєкт", "Джерело", "Цитата",
    }
    # Опції мають збігатися з тим, що пише create_task, інакше створена
    # цією ж командою база не прийме жодної задачі.
    statuses = [o["name"] for o in props["Статус"]["select"]["options"]]
    assert statuses == ["Не почато", "В роботі", "Готово", "Відкладено", "Скасовано"]
    priorities = [o["name"] for o in props["Пріоритет"]["select"]["options"]]
    assert priorities == ["Високий", "Середній", "Низький"]
    assert notion.database_id == "db-new"
