"""Чернетка відгуку: правила промпта і поведінка при збоях."""
from __future__ import annotations

from monitor.drafter import Drafter
from monitor.freelancehunt import to_project
from monitor.scoring import score_project

from .fakes import FakeAnthropic, FakeParsed, api_error, draft_response, fh_item


def make(cfg, client, **kw):
    project = to_project(fh_item(
        1, kw.pop("name", "Бот для розбору зустрічей"),
        kw.pop("desc", "Транскрипція дзвінка → задачі в Notion і Telegram"), **kw
    ))
    return Drafter(cfg, client=client).draft(project, score_project(project, cfg))


class TestHappyPath:
    def test_returns_all_bid_parts(self, cfg):
        draft = make(cfg, FakeAnthropic([draft_response()]))
        assert draft.error == ""
        assert draft.opening.startswith("У вас")
        assert draft.package == "Команда"
        assert "10 000" in draft.price
        assert draft.question.endswith("?")

    def test_offer_is_in_the_system_prompt(self, cfg):
        client = FakeAnthropic([draft_response()])
        make(cfg, client)
        system = client.calls[0]["system"]
        assert "4 000 / 10 000 / 20 000" in system
        assert "<offer>" in system

    def test_posting_is_fenced_as_untrusted(self, cfg):
        client = FakeAnthropic([draft_response()])
        make(cfg, client)
        content = client.calls[0]["messages"][0]["content"]
        assert content.startswith("<posting>")
        assert "</posting>" in content

    def test_model_and_effort_are_sent(self, cfg):
        cfg.effort = "high"
        client = FakeAnthropic([draft_response()])
        make(cfg, client)
        assert client.calls[0]["model"] == "claude-opus-5"
        assert client.calls[0]["output_config"] == {"effort": "high"}

    def test_long_description_is_truncated(self, cfg):
        client = FakeAnthropic([draft_response()])
        make(cfg, client, desc="Notion telegram зустріч " + "х" * 20000)
        content = client.calls[0]["messages"][0]["content"]
        assert len(content) < 5000


class TestMismatchIsSurfaced:
    def test_no_fit_is_flagged_for_the_human(self, cfg):
        draft = make(cfg, FakeAnthropic([draft_response(fits="no", note="просять Laravel")]))
        assert "не наша задача" in draft.note
        assert "Laravel" in draft.note

    def test_partial_fit_is_flagged(self, cfg):
        draft = make(cfg, FakeAnthropic([draft_response(fits="partly", note="потрібен Slack")]))
        assert draft.note.startswith("Частковий збіг")


class TestFailureModes:
    def test_api_error_does_not_raise(self, cfg):
        draft = make(cfg, FakeAnthropic([api_error(500)]))
        assert draft.error != ""
        assert draft.opening == ""

    def test_refusal_is_reported(self, cfg):
        draft = make(cfg, FakeAnthropic([FakeParsed(None, stop_reason="refusal")]))
        assert "відмовилася" in draft.error

    def test_missing_output_is_reported(self, cfg):
        draft = make(cfg, FakeAnthropic([FakeParsed(None)]))
        assert "структуровану" in draft.error
