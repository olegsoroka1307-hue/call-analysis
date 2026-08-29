"""Спільні фікстури. Інтеграційні тести йдуть у справжній PostgreSQL (ТЗ §46.2)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres@127.0.0.1:5432/betting_poc_test",
)

TABLES = [
    "market_predictions", "model_runs", "odds_snapshots",
    "fixtures", "teams", "leagues", "bookmakers",
]


@pytest.fixture(scope="session")
def engine():
    engine = create_engine(TEST_DATABASE_URL, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL недоступний за {TEST_DATABASE_URL}: {exc}")

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        check=True,
        capture_output=True,
    )
    return engine


@pytest.fixture
def session(engine):
    """Чиста база на кожен тест. TRUNCATE не блокується append-only тригером."""
    with engine.begin() as connection:
        connection.execute(
            text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
        )
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        yield session


@pytest.fixture(scope="session")
def replay_data_dir(tmp_path_factory) -> str:
    """Свіжий replay-датасет на кожен прогін тестів.

    Закомічений датасет у `data/` має абсолютні мітки часу, тому він
    старіє: через 15 хвилин після генерації data-guard (ТЗ §22) чесно
    визнає всі ціни простроченими і кожне рішення стає PASS. Тести, які
    цього не враховують, починають падати «за годинником», а не за кодом.

    Тому тести генерують власний датасет у тимчасовий каталог. Робочий
    `data/` лишається для демо і для compose, який теж перегенеровує його
    перед запуском.
    """
    out_dir = tmp_path_factory.mktemp("replay")
    subprocess.run(
        [sys.executable, "-m", "app.tools.make_replay_dataset",
         "--days-ahead", "3", "--out", str(out_dir)],
        cwd=BACKEND_DIR,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=True,
        capture_output=True,
    )
    return str(out_dir)
