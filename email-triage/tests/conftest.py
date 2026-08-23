from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triage.config import Config  # noqa: E402
from triage.errorlog import ErrorLog  # noqa: E402
from triage.state import State  # noqa: E402

BUSINESS = """\
Маркетингова агенція, 8 людей.
Важливо: клієнти, гроші, рахунки, дедлайни, юридичне.
Не важливо: розсилки, вебінари, холодні пропозиції.
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        business_context=BUSINESS,
        output="csv",
        csv_path=str(tmp_path / "results.csv"),
        state_db=str(tmp_path / "state.db"),
        error_log=str(tmp_path / "errors.jsonl"),
        confidence_threshold=0.75,
        fetch_links=True,
        read_pdfs=True,
    )


@pytest.fixture
def errlog(tmp_path: Path) -> ErrorLog:
    return ErrorLog(tmp_path / "errors.jsonl")


@pytest.fixture
def state(tmp_path: Path):
    st = State(tmp_path / "state.db")
    yield st
    st.close()
