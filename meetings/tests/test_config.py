from __future__ import annotations

import pytest

from meetings.config import Config, ConfigError, load_dotenv

GOOD = """
team_context: |
  Саша — продажі.
notion_database_id: "db-1"
effort: "medium"
"""


def _write(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_and_keeps_defaults(tmp_path):
    cfg = Config.load(_write(tmp_path, GOOD))
    assert cfg.notion_database_id == "db-1"
    assert cfg.effort == "medium"
    assert cfg.model == "claude-opus-5"
    assert cfg.send_telegram is True


def test_missing_file_says_what_to_do(tmp_path):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        Config.load(tmp_path / "нема.yaml")


def test_typo_in_field_name_is_not_ignored(tmp_path):
    # Мовчки проігнорувати друкарську помилку — найгірше, що можна зробити:
    # власник буде впевнений, що налаштування діє.
    with pytest.raises(ConfigError, match="notion_databse_id"):
        Config.load(_write(tmp_path, GOOD + '\nnotion_databse_id: "x"\n'))


def test_empty_team_context_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="team_context"):
        Config.load(_write(tmp_path, 'team_context: "   "\n'))


def test_safe_mode_cannot_be_turned_off(tmp_path):
    with pytest.raises(ConfigError, match="safe_mode"):
        Config.load(_write(tmp_path, GOOD + "\nsafe_mode: false\n"))


def test_unknown_effort_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="effort"):
        Config.load(_write(tmp_path, GOOD.replace('"medium"', '"величезний"')))


def test_unknown_priority_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="default_priority"):
        Config.load(_write(tmp_path, GOOD + '\ndefault_priority: "Срочный"\n'))


def test_dotenv_does_not_overwrite_real_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "справжній")
    env = tmp_path / ".env"
    env.write_text("NOTION_API_KEY=з-файлу\nTELEGRAM_BOT_TOKEN='123:abc'\n", encoding="utf-8")
    load_dotenv(env)
    import os

    assert os.environ["NOTION_API_KEY"] == "справжній"
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "123:abc"


def test_zero_max_commitments_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="max_commitments"):
        Config.load(_write(tmp_path, GOOD + "\nmax_commitments: 0\n"))


def test_example_config_is_loadable():
    # Перше, що робить власник, — копіює приклад. Якщо приклад не вантажиться,
    # він упреться в помилку ще до першого запуску.
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg = Config.load(example)
    assert cfg.team_context.strip()
    assert cfg.default_priority in ("Високий", "Середній", "Низький")
