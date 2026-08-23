#!/usr/bin/env python3
"""Точка входу. Запуск: python run.py"""
from __future__ import annotations

import argparse
import logging
import sys

from triage.classifier import Classifier
from triage.config import Config, ConfigError, api_key, load_dotenv
from triage.errorlog import ErrorLog
from triage.pipeline import run
from triage.sinks import CsvSink, SheetsSink
from triage.state import State


def build_sink(cfg: Config):
    if cfg.output == "csv":
        return CsvSink(cfg.csv_path)
    from googleapiclient.discovery import build as build_service

    from triage.gmail_source import authorize

    service = build_service(
        "sheets", "v4", credentials=authorize(cfg), cache_discovery=False
    )
    return SheetsSink(cfg.spreadsheet_id, cfg.sheet_name, service)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Тріаж вхідної пошти. Працює лише на читання."
    )
    parser.add_argument("--config", default="config.yaml", help="шлях до config.yaml")
    parser.add_argument("--limit", type=int, default=None, help="скільки листів узяти")
    parser.add_argument(
        "--auth", action="store_true",
        help="лише пройти вхід у Google і зберегти токен, без обробки",
    )
    parser.add_argument("--verbose", action="store_true", help="докладний лог")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    load_dotenv()

    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        print(f"\n❌ Проблема з налаштуваннями:\n   {exc}\n", file=sys.stderr)
        return 2

    from triage.gmail_source import GmailSource, authorize

    if args.auth:
        authorize(cfg)
        print("✅ Доступ до Google збережено. Тепер запускайте: python run.py")
        return 0

    try:
        key = api_key()
    except ConfigError as exc:
        print(f"\n❌ {exc}\n", file=sys.stderr)
        return 2

    import anthropic

    errlog = ErrorLog(cfg.error_log)
    state = State(cfg.state_db)
    try:
        source = GmailSource(cfg)
        classifier = Classifier(cfg, client=anthropic.Anthropic(api_key=key))
        sink = build_sink(cfg)
        report = run(cfg, source, classifier, sink, state, errlog, limit=args.limit)
    finally:
        state.close()

    print(report.render())
    print(errlog.summary())
    if report.labels.get("NEEDS_HUMAN_REVIEW"):
        print(
            f"\n⚠️  {report.labels['NEEDS_HUMAN_REVIEW']} лист(ів) чекають на ваш "
            f"перегляд — вони позначені NEEDS_HUMAN_REVIEW у звіті."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
