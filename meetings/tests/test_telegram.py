from __future__ import annotations

import pytest

from meetings.models import CANCELLED, DONE, POSTPONED, Commitment
from meetings.telegram import (
    MAX_TEXT, TelegramClient, TelegramError, pack_callback, restore_uuid,
    task_keyboard, task_message, unpack_callback,
)
from .fakes import FakeResponse, FakeSession

PAGE_ID = "1a2b3c4d5e6f47788990aabbccddeeff"


def client(responses=None) -> tuple[TelegramClient, FakeSession]:
    session = FakeSession(responses)
    return TelegramClient("123:abc", session=session), session


def commitment(**kwargs) -> Commitment:
    base = dict(
        responsible="Саша", task="Надіслати КП", deadline="2026-08-28",
        priority="Высокий", project="Продажі", quote="Скину до четверга.",
        page_id=PAGE_ID,
    )
    base.update(kwargs)
    return Commitment(**base)


# ── кнопки ──────────────────────────────────────────────────────────
def test_callback_data_fits_the_64_byte_limit():
    for action in ("d", "p", "x"):
        data = pack_callback(action, "1a2b3c4d-5e6f-4778-8990-aabbccddeeff")
        assert len(data.encode()) <= 64


@pytest.mark.parametrize("action, status", [("d", DONE), ("p", POSTPONED), ("x", CANCELLED)])
def test_pack_and_unpack_round_trip(action, status):
    parsed_status, page_id = unpack_callback(pack_callback(action, PAGE_ID))
    assert parsed_status == status
    assert page_id == PAGE_ID


def test_unknown_callback_data_is_ignored_not_guessed():
    assert unpack_callback("z|whatever") == ("", "whatever")
    assert unpack_callback("") == ("", "")


def test_unknown_action_cannot_be_packed():
    with pytest.raises(TelegramError):
        pack_callback("z", PAGE_ID)


def test_restore_uuid_gives_notion_back_its_dashes():
    assert restore_uuid(PAGE_ID) == "1a2b3c4d-5e6f-4778-8990-aabbccddeeff"
    assert restore_uuid("вже-з-дефісами") == "вже-з-дефісами"


def test_keyboard_has_three_buttons():
    buttons = task_keyboard(PAGE_ID)["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["✅ Готово", "🕐 Відкласти", "❌ Не актуально"]


# ── текст повідомлення ──────────────────────────────────────────────
def test_message_contains_the_essentials():
    text = task_message(commitment())
    assert "Надіслати КП" in text
    assert "2026-08-28" in text
    assert "🔴" in text
    assert "Скину до четверга." in text


def test_html_in_the_transcript_cannot_break_the_message():
    text = task_message(commitment(task="Полагодити <b>сайт</b> & форму"))
    assert "&lt;b&gt;" in text and "&amp;" in text


def test_very_long_message_is_trimmed():
    text = task_message(commitment(quote="я" * 6000))
    assert len(text) == MAX_TEXT


# ── розсилка ────────────────────────────────────────────────────────
def test_summary_groups_tasks_by_person(registry):
    registry.add("Саша Петренко", 111)
    registry.add("Марія", 222)
    telegram, session = client([FakeResponse(200, {"ok": True, "result": {}})] * 5)

    deliveries = telegram.send_meeting_summary(
        "Планірка",
        [
            commitment(responsible="Саша", task="КП"),
            commitment(responsible="Саша", task="Дзвінок"),
            commitment(responsible="Марія", task="Акт"),
        ],
        registry,
    )
    assert {d.name: d.sent for d in deliveries} == {"Саша Петренко": 2, "Марія": 1}
    assert all(d.reached for d in deliveries)
    # 2 привітання + 3 задачі
    assert len(session.requests) == 5


def test_person_not_in_the_registry_is_reported_not_skipped(registry):
    telegram, session = client()
    deliveries = telegram.send_meeting_summary("Планірка", [commitment()], registry)
    assert len(deliveries) == 1
    assert deliveries[0].reached is False
    assert "/start" in deliveries[0].error
    assert session.requests == []


def test_one_failed_task_does_not_stop_the_others(registry):
    registry.add("Саша", 111)
    telegram, _ = client([
        FakeResponse(200, {"ok": True, "result": {}}),                    # привітання
        FakeResponse(403, {"ok": False, "description": "bot was blocked"}),
        FakeResponse(200, {"ok": True, "result": {}}),
    ])
    deliveries = telegram.send_meeting_summary(
        "Планірка",
        [commitment(task="перша"), commitment(task="друга")],
        registry,
    )
    assert deliveries[0].sent == 1
    assert "403" in deliveries[0].error


def test_task_without_page_id_is_sent_without_buttons(registry):
    registry.add("Саша", 111)
    telegram, session = client([FakeResponse(200, {"ok": True, "result": {}})] * 2)
    telegram.send_meeting_summary("Планірка", [commitment(page_id="")], registry)
    assert "reply_markup" not in session.requests[1]["json"]


# ── помилки ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "status, body, expected",
    [
        (401, {"ok": False, "description": "unauthorized"}, "TELEGRAM_BOT_TOKEN"),
        (403, {"ok": False, "description": "blocked"}, "/start"),
        (429, {"ok": False, "description": "too many", "parameters": {"retry_after": 7}}, "7"),
        (500, {"ok": False, "description": "server"}, "500"),
    ],
)
def test_errors_explain_what_to_do(status, body, expected):
    telegram, _ = client([FakeResponse(status, body)])
    with pytest.raises(TelegramError, match=expected):
        telegram.send(111, "привіт")


def test_network_failure_is_wrapped():
    import requests

    telegram, _ = client([requests.ConnectionError("немає мережі")])
    with pytest.raises(TelegramError, match="зʼєднатися з Telegram"):
        telegram.send(111, "привіт")


# ── реєстрація ──────────────────────────────────────────────────────
def test_collect_contacts_dedupes_by_chat_id():
    updates = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {
                "id": 111, "first_name": "Саша", "last_name": "Петренко", "username": "sasha_p"}}},
            {"update_id": 2, "message": {"chat": {"id": 111, "first_name": "Саша"}}},
            {"update_id": 3, "message": {"chat": {"id": 222, "username": "maria"}}},
            {"update_id": 4, "callback_query": {"id": "cb"}},
        ],
    }
    telegram, _ = client([FakeResponse(200, updates)])
    contacts = telegram.collect_contacts()
    assert {c["chat_id"] for c in contacts} == {111, 222}
    assert next(c for c in contacts if c["chat_id"] == 222)["name"] == "maria"
