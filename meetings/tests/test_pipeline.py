from __future__ import annotations

import json
from datetime import date

from meetings.extractor import Extractor
from meetings.models import DONE
from meetings.pipeline import handle_callbacks, process_meeting
from meetings.registry import transcript_fingerprint
from meetings.telegram import pack_callback
from .fakes import (
    FakeAnthropic, FakeNotion, FakeTelegram, api_status_error, callback_update,
    commitment_schema, extraction,
)

TODAY = date(2026, 8, 24)
TRANSCRIPT = "Саша: я скину КП до четверга. Марія: підготую акт."
PAGE_ID = "1a2b3c4d5e6f47788990aabbccddeeff"


def two_commitments():
    return extraction(commitments=[
        commitment_schema(responsible="Саша", task="Надіслати КП"),
        commitment_schema(responsible="Марія", task="Підготувати акт", priority="Средний"),
    ])


def run(cfg, registry, errlog, *, script=None, notion=None, telegram=None, **kwargs):
    extractor = Extractor(cfg, client=FakeAnthropic(script=script or [two_commitments()]))
    return process_meeting(
        cfg, TRANSCRIPT, extractor, registry, errlog,
        notion=notion, telegram=telegram, today=TODAY, **kwargs
    )


def test_tasks_reach_notion_and_people(cfg, registry, errlog):
    registry.add("Саша", 111)
    registry.add("Марія", 222)
    notion, telegram = FakeNotion(), FakeTelegram()

    result = run(cfg, registry, errlog, notion=notion, telegram=telegram)

    assert result.created_in_notion == 2
    assert [c.task for c in notion.created] == ["Надіслати КП", "Підготувати акт"]
    assert all(c.page_id for c in result.commitments)
    assert {d.name: d.sent for d in result.deliveries} == {"Саша": 1, "Марія": 1}
    assert result.degraded is False
    assert result.unreached == []


def test_the_same_meeting_is_not_processed_twice(cfg, registry, errlog):
    notion, telegram = FakeNotion(), FakeTelegram()
    run(cfg, registry, errlog, notion=notion, telegram=telegram)
    second = run(cfg, registry, errlog, notion=notion, telegram=telegram)

    assert second.skipped_duplicate is True
    assert len(notion.created) == 2                 # нічого не подвоїлось
    assert "вже розбиралася" in "\n".join(second.lines())


def test_force_reprocesses_on_purpose(cfg, registry, errlog):
    notion = FakeNotion()
    run(cfg, registry, errlog, notion=notion)
    second = run(cfg, registry, errlog, notion=notion, force=True)
    assert second.skipped_duplicate is False
    assert len(notion.created) == 4


def test_dry_run_creates_nothing_and_says_so(cfg, registry, errlog):
    notion, telegram = FakeNotion(), FakeTelegram()
    result = run(cfg, registry, errlog, notion=notion, telegram=telegram, dry_run=True)

    assert len(result.commitments) == 2
    assert notion.created == [] and telegram.sent == []
    assert result.created_in_notion == 0
    assert "пробний запуск" in result.degraded_reason
    # Пробний запуск не має вважатися розібраною нарадою.
    assert registry.seen_meeting(transcript_fingerprint(TRANSCRIPT)) is False


def test_notion_failure_does_not_stop_the_delivery(cfg, registry, errlog):
    registry.add("Саша", 111)
    registry.add("Марія", 222)
    notion = FakeNotion(fail_on={"Надіслати КП"})
    telegram = FakeTelegram()

    result = run(cfg, registry, errlog, notion=notion, telegram=telegram)

    assert result.created_in_notion == 1
    assert result.degraded is True
    assert "Notion" in result.degraded_reason
    assert sum(d.sent for d in result.deliveries) == 2   # обидві задачі дійшли
    assert errlog.count == 1


def test_task_without_notion_page_still_goes_out_without_buttons(cfg, registry, errlog):
    registry.add("Саша", 111)
    notion = FakeNotion(fail_on={"Надіслати КП"})
    telegram = FakeTelegram()
    script = [extraction(commitments=[commitment_schema(responsible="Саша", task="Надіслати КП")])]

    run(cfg, registry, errlog, script=script, notion=notion, telegram=telegram)

    assert [keyboard for _, _, keyboard in telegram.sent] == [None, None]


def test_without_notion_the_result_warns_about_buttons(cfg, registry, errlog):
    registry.add("Саша", 111)
    registry.add("Марія", 222)
    telegram = FakeTelegram()
    result = run(cfg, registry, errlog, notion=None, telegram=telegram)
    assert result.degraded is True
    assert "кнопки не працюватимуть" in result.degraded_reason
    assert sum(d.sent for d in result.deliveries) == 2


def test_telegram_failure_keeps_the_notion_tasks(cfg, registry, errlog):
    registry.add("Саша", 111)
    notion = FakeNotion()
    result = run(cfg, registry, errlog, notion=notion, telegram=FakeTelegram(raise_on_send=True))

    assert result.created_in_notion == 2
    assert result.degraded is True
    assert errlog.count >= 1


def test_unknown_person_is_named_in_the_summary_and_the_log(cfg, registry, errlog):
    registry.add("Саша", 111)                      # Марії в реєстрі немає
    result = run(cfg, registry, errlog, notion=FakeNotion(), telegram=FakeTelegram())

    assert [d.name for d in result.unreached] == ["Марія"]
    assert "НЕ дійшло до Марія" in "\n".join(result.lines())
    entries = [json.loads(line) for line in open(errlog.path, encoding="utf-8")]
    assert any("Марія" in e["message"] for e in entries)


def test_open_questions_reach_the_summary(cfg, registry, errlog):
    script = [extraction(commitments=[], unclear=["Хто відповідальний за презентацію?"])]
    result = run(cfg, registry, errlog, script=script, notion=FakeNotion())
    assert "ПОТРІБНА ВАША ВІДПОВІДЬ" in "\n".join(result.lines())


def test_extractor_failure_is_reported_not_swallowed(cfg, registry, errlog):
    result = run(cfg, registry, errlog, script=[api_status_error(500)], notion=FakeNotion())
    assert result.degraded is True
    assert "не вдалося розібрати" in result.degraded_reason
    assert result.commitments == []
    assert errlog.count == 1
    # Провалену нараду треба мати змогу запустити знову без --force.
    assert registry.seen_meeting(transcript_fingerprint(TRANSCRIPT)) is False


def test_telegram_is_skipped_when_turned_off(cfg, registry, errlog):
    cfg.send_telegram = False
    telegram = FakeTelegram()
    result = run(cfg, registry, errlog, notion=FakeNotion(), telegram=telegram)
    assert telegram.sent == [] and result.deliveries == []


# ── кнопки ──────────────────────────────────────────────────────────
def test_button_press_updates_notion_and_removes_the_buttons(errlog):
    notion = FakeNotion()
    telegram = FakeTelegram(updates=[callback_update(7, pack_callback("d", PAGE_ID))])

    handled, offset = handle_callbacks(telegram, notion, errlog, timeout=0)

    assert handled == 1
    assert offset == 8
    assert notion.statuses == [("1a2b3c4d-5e6f-4778-8990-aabbccddeeff", DONE)]
    assert telegram.answers == [("cb-7", f"Записав: {DONE}")]
    assert telegram.cleared == [(10, 5)]


def test_failed_status_update_tells_the_person(errlog):
    notion = FakeNotion(fail_on={"1a2b3c4d-5e6f-4778-8990-aabbccddeeff"})
    telegram = FakeTelegram(updates=[callback_update(7, pack_callback("d", PAGE_ID))])

    handled, _ = handle_callbacks(telegram, notion, errlog, timeout=0)

    assert handled == 0
    assert "Не вдалося" in telegram.answers[0][1]
    assert telegram.cleared == []            # кнопки лишаються, щоб можна було повторити
    assert errlog.count == 1


def test_foreign_callback_data_is_ignored(errlog):
    notion = FakeNotion()
    telegram = FakeTelegram(updates=[callback_update(7, "z|щось-чуже")])
    handled, offset = handle_callbacks(telegram, notion, errlog, timeout=0)
    assert (handled, offset) == (0, 8)
    assert notion.statuses == []


def test_plain_messages_only_move_the_offset(errlog):
    telegram = FakeTelegram(updates=[{"update_id": 3, "message": {"chat": {"id": 1}}}])
    handled, offset = handle_callbacks(telegram, FakeNotion(), errlog, timeout=0)
    assert (handled, offset) == (0, 4)


def test_created_but_undelivered_task_is_flagged(cfg, registry, errlog):
    # Задача є в Notion, а людина про неї не знає — це має бути видно одразу,
    # а не через тиждень у звіті.
    registry.add("Саша", 111)
    result = run(cfg, registry, errlog, notion=FakeNotion(), telegram=FakeTelegram())
    assert result.created_in_notion == 2
    assert result.degraded is True
    assert "не дійшли до: Марія" in result.degraded_reason


def test_several_failures_are_all_named(cfg, registry, errlog):
    notion = FakeNotion(fail_on={"Надіслати КП"})
    result = run(cfg, registry, errlog, notion=notion, telegram=FakeTelegram())
    assert "Notion" in result.degraded_reason
    assert "не дійшли до" in result.degraded_reason
