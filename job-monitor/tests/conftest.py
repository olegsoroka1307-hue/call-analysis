from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor.config import Config  # noqa: E402
from monitor.errorlog import ErrorLog  # noqa: E402
from monitor.state import State  # noqa: E402

OFFER = """\
Налаштовую AI-автоматизації. Розбір зустрічей → задачі в Notion і Telegram,
пакети 4 000 / 10 000 / 20 000 ₴. Тріаж вхідної пошти, 6 000 / 12 000 ₴.
Не роблю: дизайн, тексти, SEO.
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        my_offer=OFFER,
        csv_path=str(tmp_path / "leads.csv"),
        state_db=str(tmp_path / "state.db"),
        error_log=str(tmp_path / "errors.jsonl"),
    )


@pytest.fixture
def errlog(tmp_path: Path) -> ErrorLog:
    return ErrorLog(tmp_path / "errors.jsonl")


@pytest.fixture
def state(tmp_path: Path):
    st = State(tmp_path / "state.db")
    yield st
    st.close()
