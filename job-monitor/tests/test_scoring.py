"""Відсів: що показувати, а що ні."""
from __future__ import annotations

from monitor.freelancehunt import to_project
from monitor.scoring import score_project

from .fakes import fh_item


def score(cfg, **kw):
    return score_project(to_project(fh_item(1, kw.pop("name", "Проєкт"),
                                            kw.pop("desc", ""), **kw)), cfg)


class TestRelevance:
    def test_meeting_project_scores_high_and_maps_to_the_right_product(self, cfg):
        s = score(cfg, name="Бот для розбору зустрічей",
                  desc="Треба автоматично витягувати задачі з транскрипції "
                       "дзвінка і кидати в Notion та Telegram")
        assert s.passed
        assert s.total >= 12
        assert s.product == "meetings"

    def test_email_project_maps_to_email_product(self, cfg):
        s = score(cfg, name="Автоматизація вхідної пошти",
                  desc="Сортувати листи в Gmail, витягувати важливе, "
                       "складати звіт у Google Sheets")
        assert s.passed
        assert s.product == "email"

    def test_generic_automation_still_passes(self, cfg):
        s = score(cfg, name="Інтеграція CRM через API",
                  desc="Потрібна автоматизація процесів, можливо n8n або zapier")
        assert s.passed
        assert s.product == "automation"

    def test_unrelated_project_is_filtered_out(self, cfg):
        s = score(cfg, name="Намалювати логотип",
                  desc="Потрібен логотип і банер для сайту")
        assert not s.passed
        assert "нижча за поріг" in s.blocked_by

    def test_stop_words_subtract(self, cfg):
        with_stop = score(cfg, name="Автоматизація і верстка",
                          desc="Потрібна автоматизація, а ще верстк сторінок і копірайт")
        without = score(cfg, name="Автоматизація", desc="Потрібна автоматизація")
        assert with_stop.total < without.total

    def test_repeated_keywords_do_not_inflate_score_without_limit(self, cfg):
        spam = score(cfg, name="notion notion notion",
                     desc="notion " * 50)
        honest = score(cfg, name="Інтеграція Notion і Telegram",
                       desc="Потрібна автоматизація задач через notion і telegram-бот")
        assert honest.total >= spam.total

    def test_matched_words_are_reported_for_the_human(self, cfg):
        s = score(cfg, name="Telegram-бот", desc="автоматизація задач у notion")
        assert "telegram" in s.matched
        assert "notion" in s.matched


class TestGates:
    def test_too_many_bids_is_too_late(self, cfg):
        cfg.max_bids = 5
        s = score(cfg, name="Автоматизація зустрічей у notion",
                  desc="транскрипція, telegram", bid_count=40)
        assert not s.passed
        assert "запізно" in s.blocked_by

    def test_low_budget_is_skipped(self, cfg):
        s = score(cfg, name="Автоматизація зустрічей notion telegram",
                  desc="транскрипція", amount=500)
        assert not s.passed
        assert "бюджет" in s.blocked_by

    def test_missing_budget_is_not_a_reason_to_skip(self, cfg):
        s = score(cfg, name="Автоматизація зустрічей notion telegram",
                  desc="транскрипція дзвінків", amount=None)
        assert s.passed

    def test_unknown_currency_is_not_a_reason_to_skip(self, cfg):
        s = score(cfg, name="Автоматизація зустрічей notion telegram",
                  desc="транскрипція", amount=10, currency="PLN")
        assert s.passed

    def test_plus_only_is_skipped_when_you_have_no_plus(self, cfg):
        s = score(cfg, name="Автоматизація зустрічей notion telegram",
                  desc="транскрипція", only_for_plus=True)
        assert not s.passed
        assert "Plus" in s.blocked_by

    def test_plus_only_passes_when_you_have_plus(self, cfg):
        cfg.skip_plus_only = False
        s = score(cfg, name="Автоматизація зустрічей notion telegram",
                  desc="транскрипція дзвінків", only_for_plus=True)
        assert s.passed
