from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meetings.config import Config  # noqa: E402
from meetings.errorlog import ErrorLog  # noqa: E402
from meetings.registry import Registry  # noqa: E402

TEAM = "Саша — продажі. Марія — фінанси. Петро — сайт."


@pytest.fixture
def cfg(tmp_path) -> Config:
    config = Config(
        team_context=TEAM,
        notion_database_id="db-1",
        registry_db=str(tmp_path / "registry.db"),
        error_log=str(tmp_path / "errors.jsonl"),
    )
    config.validate()
    return config


@pytest.fixture
def registry(cfg) -> Registry:
    reg = Registry(cfg.registry_db)
    yield reg
    reg.close()


@pytest.fixture
def errlog(cfg) -> ErrorLog:
    return ErrorLog(cfg.error_log)
