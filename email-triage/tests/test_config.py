"""Конфігурація має ловити помилки власника зрозумілим текстом."""
from __future__ import annotations

import pytest

from triage.config import Config, ConfigError, load_dotenv


def base(**kw) -> Config:
    return Config(business_context="Агенція. Важливо: клієнти.", **kw)


def test_empty_business_context_is_refused():
    with pytest.raises(ConfigError, match="business_context"):
        Config(business_context="   ").validate()


def test_sheets_without_spreadsheet_id_is_refused():
    with pytest.raises(ConfigError, match="spreadsheet_id"):
        base(output="sheets").validate()


def test_unknown_output_is_refused():
    with pytest.raises(ConfigError, match="csv"):
        base(output="telegram").validate()


def test_bad_threshold_is_refused():
    with pytest.raises(ConfigError, match="confidence_threshold"):
        base(confidence_threshold=1.5).validate()


def test_bad_effort_is_refused():
    with pytest.raises(ConfigError, match="effort"):
        base(effort="дуже").validate()


def test_sheets_output_adds_sheets_scope():
    cfg = base(output="sheets", spreadsheet_id="abc123")
    cfg.validate()
    assert any("spreadsheets" in s for s in cfg.scopes)
    assert any(s.endswith("gmail.readonly") for s in cfg.scopes)


def test_typo_in_yaml_field_is_reported(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "business_context: |\n  Агенція\ngmail_querry: 'is:unread'\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="gmail_querry"):
        Config.load(path)


def test_missing_config_file_explains_what_to_do(tmp_path):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        Config.load(tmp_path / "nope.yaml")


def test_valid_yaml_loads(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "business_context: |\n  Агенція. Важливо: гроші.\n"
        "gmail_query: 'in:inbox newer_than:1d'\nmax_emails_per_run: 5\n",
        encoding="utf-8",
    )
    cfg = Config.load(path)
    assert cfg.gmail_query == "in:inbox newer_than:1d"
    assert cfg.max_emails_per_run == 5
    assert cfg.safe_mode is True


def test_dotenv_reader(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('ANTHROPIC_API_KEY="sk-ant-test"\n# коментар\nEMPTY\n', encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    load_dotenv(env)
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"
