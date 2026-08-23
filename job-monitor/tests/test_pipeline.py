"""Наскрізний прохід: біржа → відсів → чернетка → сповіщення."""
from __future__ import annotations

import csv

from monitor.drafter import Drafter
from monitor.freelancehunt import FreelancehuntClient
from monitor.notify import CsvSink, TelegramNotifier, format_lead
from monitor.pipeline import run

from .fakes import (
    FakeAnthropic, FakeSession, MemoryNotifier, api_error, draft_response, fh_item,
)

GOOD = fh_item(
    101, "Автоматизація розбору зустрічей",
    "Треба з транскрипції дзвінка витягувати задачі й кидати в Notion "
    "та Telegram співробітникам",
    skills=["Python", "Telegram"], amount=18000, bid_count=2,
)
JUNK = fh_item(
    102, "Намалювати логотип", "Логотип і банер для сайту", amount=2000
)
LATE = fh_item(
    103, "Бот Telegram notion автоматизація зустрічей",
    "транскрипція дзвінків", amount=20000, bid_count=99,
)


def build(cfg, pages, script=None, notifiers=None, state=None, errlog=None, drafter=True):
    client = FreelancehuntClient("t", session=FakeSession(pages))
    fake_ai = FakeAnthropic(script or [])
    d = Drafter(cfg, client=fake_ai) if drafter else None
    notifiers = notifiers if notifiers is not None else [MemoryNotifier()]
    report, leads = run(cfg, client, state, errlog, notifiers, d)
    return report, leads, notifiers, fake_ai


class TestEndToEnd:
    def test_relevant_project_reaches_the_notifier_with_a_draft(self, cfg, state, errlog):
        report, leads, notifiers, _ = build(
            cfg, [[GOOD, JUNK, LATE], []], [draft_response()], state=state, errlog=errlog
        )
        assert report.fetched == 3
        assert report.leads == 1
        assert report.drafted == 1
        assert len(notifiers[0].sent) == 1
        lead = notifiers[0].sent[0]
        assert lead.project.project_id == "101"
        assert lead.draft.package == "Команда"

    def test_filtered_projects_are_counted_by_reason(self, cfg, state, errlog):
        report, _, _, _ = build(
            cfg, [[GOOD, JUNK, LATE], []], [draft_response()], state=state, errlog=errlog
        )
        assert sum(report.blocked.values()) == 2

    def test_weak_match_is_shown_but_not_drafted(self, cfg, state, errlog):
        cfg.min_score = 3
        cfg.draft_min_score = 30       # жодна чернетка не проходить поріг
        report, _, notifiers, ai = build(
            cfg, [[GOOD], []], [draft_response()], state=state, errlog=errlog
        )
        assert report.leads == 1
        assert report.drafted == 0
        assert ai.calls == []          # на ШІ не витратились

    def test_nothing_is_shown_twice(self, cfg, state, errlog):
        build(cfg, [[GOOD], []], [draft_response()], state=state, errlog=errlog)
        report2, _, notifiers2, _ = build(
            cfg, [[GOOD], []], [draft_response()], state=state, errlog=errlog
        )
        assert report2.skipped_seen == 1
        assert report2.leads == 0
        assert notifiers2[0].sent == []

    def test_rejected_projects_are_also_remembered(self, cfg, state, errlog):
        build(cfg, [[JUNK], []], state=state, errlog=errlog, drafter=False)
        report2, _, _, _ = build(cfg, [[JUNK], []], state=state, errlog=errlog, drafter=False)
        assert report2.skipped_seen == 1  # вдруге його вже не перебирали


class TestResilience:
    def test_api_failure_is_logged_and_run_ends_cleanly(self, cfg, state, errlog):
        import requests

        report, leads, _, _ = build(
            cfg, [requests.ConnectionError("down")], state=state, errlog=errlog
        )
        assert report.fetched == 0
        assert leads == []
        assert errlog.counts["api"] == 1

    def test_draft_failure_still_delivers_the_lead(self, cfg, state, errlog):
        report, _, notifiers, _ = build(
            cfg, [[GOOD], []], [api_error(500)], state=state, errlog=errlog
        )
        assert report.leads == 1
        assert report.drafted == 0
        assert len(notifiers[0].sent) == 1     # замовлення показали попри збій ШІ
        assert errlog.counts["draft"] == 1

    def test_notifier_failure_keeps_the_lead_for_the_next_run(self, cfg, state, errlog):
        broken = MemoryNotifier(fail_times=1)
        report, _, _, _ = build(
            cfg, [[GOOD], []], [draft_response()],
            notifiers=[broken], state=state, errlog=errlog
        )
        assert report.notified == 0
        assert errlog.counts["notify"] == 1
        assert state.seen("freelancehunt", "101") is False

    def test_one_working_channel_is_enough_to_mark_as_delivered(self, cfg, state, errlog):
        broken, working = MemoryNotifier(fail_times=1), MemoryNotifier()
        build(cfg, [[GOOD], []], [draft_response()],
              notifiers=[broken, working], state=state, errlog=errlog)
        assert len(working.sent) == 1
        assert state.seen("freelancehunt", "101") is True


class TestOutput:
    def test_message_carries_what_is_needed_to_decide(self, cfg, state, errlog):
        _, _, notifiers, _ = build(
            cfg, [[GOOD], []], [draft_response()], state=state, errlog=errlog
        )
        text = format_lead(notifiers[0].sent[0])
        assert "Автоматизація розбору зустрічей" in text
        assert "18 000 UAH" in text
        assert "freelancehunt.com/project" in text
        assert "ЧЕРНЕТКА ВІДГУКУ" in text

    def test_csv_gets_a_header_once_and_a_row_per_lead(self, cfg, state, errlog, tmp_path):
        sink = CsvSink(cfg.csv_path)
        build(cfg, [[GOOD], []], [draft_response()],
              notifiers=[sink], state=state, errlog=errlog)
        other = fh_item(104, "Notion telegram автоматизація зустрічей",
                        "транскрипція дзвінків у задачі", amount=9000)
        build(cfg, [[other], []], [draft_response()],
              notifiers=[sink], state=state, errlog=errlog)
        rows = list(csv.reader(open(cfg.csv_path, encoding="utf-8-sig")))
        assert rows[0][0] == "Знайдено"
        assert len(rows) == 3

    def test_telegram_message_is_capped_to_the_platform_limit(self, cfg, state, errlog):
        session = FakeSession()
        notifier = TelegramNotifier("bot-token", "123", session=session)
        long_draft = draft_response(opening="я" * 9000)
        build(cfg, [[GOOD], []], [long_draft],
              notifiers=[notifier], state=state, errlog=errlog)
        sent = session.posts[0]["json"]
        assert sent["chat_id"] == "123"
        assert len(sent["text"]) <= 4096   # жорстка межа Telegram
        assert sent["text"].endswith("…")  # і видно, що обрізано
        assert "bot-token" in session.posts[0]["url"]

    def test_telegram_rejection_explains_the_usual_cause(self, cfg, state, errlog):
        notifier = TelegramNotifier("t", "1", session=FakeSession(post_status=403))
        build(cfg, [[GOOD], []], [draft_response()],
              notifiers=[notifier], state=state, errlog=errlog)
        assert "/start" in errlog.entries[0]["error"]
