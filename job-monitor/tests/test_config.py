"""Конфігурація має пояснювати помилки словами, а не трасуванням."""
from __future__ import annotations

import pytest

from monitor.config import Config, ConfigError


def test_draft_without_offer_is_refused():
    with pytest.raises(ConfigError, match="my_offer"):
        Config(draft_replies=True, my_offer="").validate()


def test_telegram_without_chat_id_is_refused():
    with pytest.raises(ConfigError, match="telegram_chat_id"):
        Config(draft_replies=False, notify_telegram=True).validate()


def test_bad_min_score_is_refused():
    with pytest.raises(ConfigError, match="min_score"):
        Config(draft_replies=False, min_score=0).validate()


def test_too_many_pages_is_refused():
    with pytest.raises(ConfigError, match="pages"):
        Config(draft_replies=False, pages=50).validate()


def test_broken_keyword_group_is_refused():
    with pytest.raises(ConfigError, match="ключових слів"):
        Config(draft_replies=False, keywords={"bad": ["не словник"]}).validate()


def test_typo_in_yaml_is_named(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("min_scor: 5\ndraft_replies: false\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="min_scor"):
        Config.load(path)


def test_valid_yaml_loads(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "draft_replies: false\nmin_score: 9\nmax_bids: 10\n", encoding="utf-8"
    )
    cfg = Config.load(path)
    assert cfg.min_score == 9
    assert cfg.max_bids == 10
    assert cfg.keywords["meetings"]["weight"] == 5  # типові слова на місці
