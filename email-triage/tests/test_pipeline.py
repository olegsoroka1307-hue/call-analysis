"""Наскрізні сценарії: від листа в Gmail до рядка у звіті."""
from __future__ import annotations

import pytest

from triage.classifier import REVIEW, Classifier
from triage.config import GMAIL_READONLY_SCOPE, Config, ConfigError
from triage.gmail_source import GmailSource
from triage.models import SHEET_HEADER
from triage.pipeline import run
from triage.sinks import CsvSink

from .fakes import (
    FakeAnthropic,
    FakeGmailService,
    FakeHttpResponse,
    FakeSession,
    MemorySink,
    api_status_error,
    gmail_message,
    verdict_response,
)
from .fixtures.build_pdf import make_pdf

INVOICE_PDF = make_pdf(
    [
        "INVOICE No 2026-0815",
        "Client: Vector Agency LLC",
        "Service: media buying, August 2026",
        "Total: 48000 UAH",
        "Due date: 2026-08-29",
    ]
)


@pytest.fixture
def no_dns(monkeypatch):
    """Вимикає перевірку приватних адрес — у тестах мережі немає."""
    monkeypatch.setattr("triage.extract.web._resolves_to_private", lambda host: False)


def build(cfg, messages, script, *, sink=None, session=None, state=None, errlog=None):
    source = GmailSource(cfg, service=FakeGmailService(messages))
    client = FakeAnthropic(script)
    classifier = Classifier(cfg, client=client)
    report = run(
        cfg, source, classifier, sink, state, errlog, session=session
    )
    return report, client


class TestSixScenarios:
    """Шість сценаріїв, які замовник просив перевірити."""

    def test_1_ordinary_important_email(self, cfg, state, errlog):
        messages = [
            gmail_message(
                "m1", "Олена Кравець <olena@bigclient.ua>", "Затримка оплати за липень",
                "Доброго дня! У нас зависла оплата за липневий рахунок. "
                "Потрібен акт до четверга, інакше бухгалтерія не проведе.",
            )
        ]
        sink = MemorySink()
        report, _ = build(
            cfg, messages,
            [verdict_response(
                "IMPORTANT", 0.94,
                reason="чинний клієнт, гроші, є дедлайн",
                sender_summary="Олена Кравець, BigClient",
                topic="Затримка оплати за липень",
                action_required="Надіслати акт",
                deadline="2026-08-27",
            )],
            sink=sink, state=state, errlog=errlog,
        )
        assert report.labels["IMPORTANT"] == 1
        assert report.written == 1
        row = dict(zip(SHEET_HEADER, sink.rows[0]))
        assert row["Вердикт"] == "IMPORTANT"
        assert row["Що зробити"] == "Надіслати акт"
        assert row["Дедлайн"] == "2026-08-27"
        assert errlog.total == 0

    def test_2_spam(self, cfg, state, errlog):
        messages = [
            gmail_message(
                "m2", "Best Deals <promo@spammy.biz>", "🔥 Знижка 90% лише сьогодні",
                "Унікальна пропозиція! Купуйте зараз! Відпишіться тут.",
            )
        ]
        sink = MemorySink()
        report, _ = build(
            cfg, messages,
            [verdict_response("NOT_IMPORTANT", 0.97, reason="масова рекламна розсилка")],
            sink=sink, state=state, errlog=errlog,
        )
        assert report.labels["NOT_IMPORTANT"] == 1
        assert dict(zip(SHEET_HEADER, sink.rows[0]))["Вердикт"] == "NOT_IMPORTANT"

    def test_3_email_with_pdf(self, cfg, state, errlog):
        messages = [
            gmail_message(
                "m3", "Бухгалтерія <buh@partner.ua>", "Рахунок за серпень",
                "Надсилаємо рахунок. Деталі у вкладенні.",
                pdf=("invoice-2026-0815.pdf", INVOICE_PDF),
            )
        ]
        sink = MemorySink()
        report, client = build(
            cfg, messages,
            [verdict_response(
                "IMPORTANT", 0.91, topic="Рахунок 2026-0815 на 48000 грн",
                action_required="Оплатити рахунок", deadline="2026-08-29",
            )],
            sink=sink, state=state, errlog=errlog,
        )
        # Текст із PDF реально дійшов до моделі
        sent = client.calls[0]["messages"][0]["content"]
        assert "INVOICE No 2026-0815" in sent
        assert "48000 UAH" in sent
        assert "Due date: 2026-08-29" in sent
        row = dict(zip(SHEET_HEADER, sink.rows[0]))
        assert "invoice-2026-0815.pdf" in row["Вкладення/посилання"]
        assert report.labels["IMPORTANT"] == 1

    def test_4_email_with_link(self, cfg, state, errlog, no_dns):
        messages = [
            gmail_message(
                "m4", "Сервіс <noreply@tool.io>", "Зміна тарифів",
                "З 1 вересня змінюються умови: https://tool.io/pricing-2026",
            )
        ]
        page = FakeHttpResponse(
            "<html><head><title>Нові тарифи</title></head><body>"
            "<h1>Pro</h1><p>Ціна зростає з 200 до 500 доларів на місяць "
            "з 1 вересня 2026 року.</p></body></html>"
        )
        session = FakeSession({"https://tool.io/pricing-2026": page})
        sink = MemorySink()
        report, client = build(
            cfg, messages,
            [verdict_response(
                "IMPORTANT", 0.88, topic="Тариф зростає з 200 до 500 доларів",
                action_required="Вирішити, чи лишаємось на сервісі",
            )],
            sink=sink, session=session, state=state, errlog=errlog,
        )
        assert session.requested == ["https://tool.io/pricing-2026"]
        sent = client.calls[0]["messages"][0]["content"]
        assert "Нові тарифи" in sent
        assert "500 доларів" in sent  # вміст сторінки дійшов до моделі
        assert report.written == 1

    def test_5_ambiguous_email_goes_to_human(self, cfg, state, errlog):
        messages = [
            gmail_message(
                "m5", "Ihor <i.petrenko@gmail.com>", "Питання",
                "Привіт, треба обговорити те, про що говорили. Наберу пізніше.",
            )
        ]
        sink = MemorySink()
        report, _ = build(
            cfg, messages,
            [verdict_response(
                "IMPORTANT", 0.52,
                reason="незрозуміло, чи це чинний клієнт; конкретики в листі немає",
            )],
            sink=sink, state=state, errlog=errlog,
        )
        assert report.labels[REVIEW] == 1
        row = dict(zip(SHEET_HEADER, sink.rows[0]))
        assert row["Вердикт"] == REVIEW
        assert "нижче порога" in row["Чому такий вердикт"]

    def test_6_api_error_does_not_lose_the_email(self, cfg, state, errlog):
        messages = [
            gmail_message("m6", "client@co.ua", "Терміново", "Потрібна відповідь сьогодні."),
            gmail_message("m7", "news@blog.io", "Дайджест", "Новини тижня."),
        ]
        sink = MemorySink()
        report, _ = build(
            cfg, messages,
            [api_status_error(500), verdict_response("NOT_IMPORTANT", 0.95)],
            sink=sink, state=state, errlog=errlog,
        )
        # Лист, на якому впало API, все одно потрапив у звіт — і на перегляд людині
        assert report.processed == 2
        assert report.written == 2
        failed = dict(zip(SHEET_HEADER, sink.rows[0]))
        assert failed["Вердикт"] == REVIEW
        assert failed["Знижена якість"] == "так"
        assert "Не вдалося класифікувати" in failed["Чому такий вердикт"]
        # Помилка зафіксована в журналі
        assert errlog.counts["classify"] == 1
        assert "classify" in errlog.summary()
        # Другий лист обробився нормально — прогін не зупинився
        assert dict(zip(SHEET_HEADER, sink.rows[1]))["Вердикт"] == "NOT_IMPORTANT"


class TestPipelineBehaviour:
    def test_already_processed_emails_are_skipped(self, cfg, state, errlog):
        messages = [gmail_message("m1", "a@b.ua", "Тема", "Текст")]
        sink = MemorySink()
        build(cfg, messages, [verdict_response("NOT_IMPORTANT", 0.9)],
              sink=sink, state=state, errlog=errlog)
        report2, _ = build(cfg, messages, [verdict_response("NOT_IMPORTANT", 0.9)],
                           sink=sink, state=state, errlog=errlog)
        assert report2.skipped_seen == 1
        assert report2.processed == 0
        assert len(sink.rows) == 1

    def test_sink_failure_keeps_email_unprocessed_for_retry(self, cfg, state, errlog):
        messages = [gmail_message("m1", "a@b.ua", "Тема", "Текст")]
        sink = MemorySink(fail_times=1)
        report, _ = build(cfg, messages, [verdict_response("IMPORTANT", 0.9)],
                          sink=sink, state=state, errlog=errlog)
        assert report.written == 0
        assert errlog.counts["sink"] == 1
        # Стан не позначено — наступний запуск спробує ще раз, лист не загублено
        assert state.seen("m1") is False

    def test_gmail_failure_is_fatal_but_reported(self, cfg, state, errlog):
        source = GmailSource(
            cfg, service=FakeGmailService([], fail_on_list=RuntimeError("503 backend"))
        )
        report = run(cfg, source, Classifier(cfg, client=FakeAnthropic()),
                     MemorySink(), state, errlog)
        assert report.fetched == 0
        assert errlog.counts["gmail"] == 1
        assert errlog.entries[0]["fatal"] is True

    def test_broken_pdf_is_logged_and_email_still_classified(self, cfg, state, errlog):
        messages = [
            gmail_message("m1", "a@b.ua", "Договір", "У вкладенні.",
                          pdf=("broken.pdf", b"%PDF-1.4 not a real pdf"))
        ]
        sink = MemorySink()
        report, _ = build(cfg, messages, [verdict_response("NEEDS_HUMAN_REVIEW", 0.9)],
                          sink=sink, state=state, errlog=errlog)
        assert report.written == 1
        assert errlog.counts["pdf"] == 1

    def test_csv_sink_writes_header_once(self, cfg, state, errlog, tmp_path):
        messages = [gmail_message("m1", "a@b.ua", "Тема", "Текст")]
        sink = CsvSink(cfg.csv_path)
        build(cfg, messages, [verdict_response("IMPORTANT", 0.9)],
              sink=sink, state=state, errlog=errlog)
        build(cfg, [gmail_message("m2", "c@d.ua", "Друга", "Текст")],
              [verdict_response("NOT_IMPORTANT", 0.9)],
              sink=sink, state=state, errlog=errlog)
        lines = [l for l in open(cfg.csv_path, encoding="utf-8-sig").read().splitlines() if l]
        assert lines[0].startswith("Оброблено,")
        assert len(lines) == 3  # заголовок + два листи


class TestSafeMode:
    def test_only_readonly_gmail_scope_is_allowed(self, cfg):
        assert cfg.scopes == [GMAIL_READONLY_SCOPE]

    def test_widening_gmail_scope_is_refused(self, cfg, monkeypatch):
        monkeypatch.setattr(
            type(cfg), "scopes",
            property(lambda self: ["https://www.googleapis.com/auth/gmail.modify"]),
        )
        with pytest.raises(ConfigError, match="SAFE MODE"):
            GmailSource(cfg, service=FakeGmailService([]))

    def test_safe_mode_cannot_be_turned_off(self):
        cfg = Config(business_context="щось", safe_mode=False)
        with pytest.raises(ConfigError, match="safe_mode"):
            cfg.validate()

    def test_source_exposes_no_write_methods(self, cfg):
        source = GmailSource(cfg, service=FakeGmailService([]))
        public = {m for m in dir(source) if not m.startswith("_")}
        for forbidden in ("delete", "trash", "send", "modify", "reply", "forward"):
            assert not any(forbidden in name.lower() for name in public)
