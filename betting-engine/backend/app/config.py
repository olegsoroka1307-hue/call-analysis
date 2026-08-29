"""Конфігурація (ТЗ §47)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres@127.0.0.1:5432/betting_poc"

    # Провайдер odds. "replay" — офлайн-датасет, "the_odds_api" — реальний HTTP.
    odds_provider: str = "replay"
    odds_api_key: str = ""
    odds_api_base_url: str = "https://api.the-odds-api.com/v4"
    odds_api_regions: str = "eu"
    replay_data_dir: str = "data"

    default_bookmaker: str = "Stake"
    default_timezone: str = "Europe/Warsaw"
    model_version: str = "0.1.0-poisson"

    # Тестова lambda для PoC (ТЗ §52 п.7). Реальні lambda рахуються у Sprint 2.
    poc_lambda_home: float = 1.55
    poc_lambda_away: float = 1.20


@lru_cache
def get_settings() -> Settings:
    return Settings()
