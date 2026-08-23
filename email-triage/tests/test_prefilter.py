"""Префільтр масової пошти: економія має бути безпечною."""
from __future__ import annotations

from datetime import datetime, timezone

from triage.classifier import Classifier
from triage.gmail_source import GmailSource
from triage.models import Attachment, EmailMessage
from triage.pipeline import run
from triage.prefilter import looks_like_bulk, prefilter

from .fakes import (
    FakeAnthropic, FakeGmailService, MemorySink, gmail_message, verdict_response,
)
from .fixtures.build_pdf import make_pdf


def mail(headers=None, attachments=None, sender="news@service.io") -> EmailMessage:
    return EmailMessage(
        message_id="m", thread_id="t", sender=sender, subject="Дайджест",
        body="Новини тижня", received_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        attachments=attachments or [], headers=headers or {},
    )


class TestBulkDetection:
    def test_detects_list_unsubscribe(self):
        assert looks_like_bulk(mail({"List-Unsubscribe": "<https://x/u>"})) == "List-Unsubscribe"

    def test_detects_list_id(self):
        assert looks_like_bulk(mail({"List-Id": "<news.x.io>"})) == "List-Id"

    def test_detects_bulk_precedence(self):
        assert looks_like_bulk(mail({"Precedence": "bulk"})) == "Precedence: bulk"

    def test_header_case_does_not_matter(self):
        assert looks_like_bulk(mail({"list-unsubscribe": "<u>"})) is not None

    def test_ordinary_email_is_not_bulk(self):
        assert looks_like_bulk(mail({"Message-Id": "<abc>"})) is None


class TestPrefilterSafety:
    def test_skips_plain_newsletter(self):
        verdict = prefilter(mail({"List-Unsubscribe": "<u>"}), [])
        assert verdict is not None
        assert verdict.label == "NOT_IMPORTANT"
        assert verdict.degraded is True  # видно, що рішення прийняв не ШІ

    def test_never_skips_when_there_is_an_attachment(self):
        att = [Attachment("invoice.pdf", "application/pdf", 10, b"x")]
        assert prefilter(mail({"List-Unsubscribe": "<u>"}, attachments=att), []) is None

    def test_never_skips_protected_sender(self):
        email = mail({"List-Unsubscribe": "<u>"}, sender="Клієнт <ceo@bigclient.ua>")
        assert prefilter(email, ["bigclient.ua"]) is None

    def test_protected_sender_match_is_case_insensitive(self):
        email = mail({"List-Unsubscribe": "<u>"}, sender="CEO@BigClient.UA")
        assert prefilter(email, ["bigclient.ua"]) is None

    def test_never_touches_ordinary_email(self):
        assert prefilter(mail(), []) is None

    def test_blank_entries_in_protected_list_are_ignored(self):
        # Порожній рядок не повинен збігатися з усіма відправниками
        assert prefilter(mail({"List-Unsubscribe": "<u>"}), ["", "  "]) is not None


class TestPrefilterInPipeline:
    def _run(self, cfg, state, errlog, messages, script):
        sink = MemorySink()
        client = FakeAnthropic(script)
        report = run(
            cfg,
            GmailSource(cfg, service=FakeGmailService(messages)),
            Classifier(cfg, client=client),
            sink, state, errlog,
        )
        return report, client, sink

    def test_disabled_by_default_every_email_reaches_the_model(self, cfg, state, errlog):
        messages = [gmail_message("m1", "n@s.io", "Дайджест", "текст",
                                  extra_headers={"List-Unsubscribe": "<u>"})]
        report, client, _ = self._run(
            cfg, state, errlog, messages, [verdict_response("NOT_IMPORTANT", 0.9)]
        )
        assert cfg.skip_bulk_mail is False
        assert report.prefiltered == 0
        assert len(client.calls) == 1

    def test_enabled_saves_the_api_call(self, cfg, state, errlog):
        cfg.skip_bulk_mail = True
        messages = [gmail_message("m1", "n@s.io", "Дайджест", "текст",
                                  extra_headers={"List-Unsubscribe": "<u>"})]
        report, client, sink = self._run(cfg, state, errlog, messages, [])
        assert report.prefiltered == 1
        assert client.calls == []          # модель не викликалась
        assert len(sink.rows) == 1         # але лист у звіті є

    def test_newsletter_with_pdf_still_goes_to_the_model(self, cfg, state, errlog):
        cfg.skip_bulk_mail = True
        messages = [
            gmail_message("m1", "billing@s.io", "Рахунок", "у вкладенні",
                          pdf=("inv.pdf", make_pdf(["INVOICE 1", "Total: 100 UAH"])),
                          extra_headers={"List-Unsubscribe": "<u>"})
        ]
        report, client, _ = self._run(
            cfg, state, errlog, messages, [verdict_response("IMPORTANT", 0.9)]
        )
        assert report.prefiltered == 0
        assert len(client.calls) == 1
        assert "INVOICE 1" in client.calls[0]["messages"][0]["content"]
