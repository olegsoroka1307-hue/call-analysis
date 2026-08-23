#!/usr/bin/env python3
"""Монітор нових замовлень. Запуск: python run.py"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from monitor.config import Config, ConfigError, load_dotenv, require
from monitor.errorlog import ErrorLog
from monitor.freelancehunt import FreelancehuntClient
from monitor.notify import ConsoleNotifier, CsvSink, TelegramNotifier
from monitor.pipeline import run as run_once
from monitor.state import State


def build_notifiers(cfg: Config) -> list:
    notifiers: list = [ConsoleNotifier(), CsvSink(cfg.csv_path)]
    if cfg.notify_telegram:
        token = require(
            "TELEGRAM_BOT_TOKEN",
            "Створіть бота через @BotFather і покладіть токен у .env.",
        )
        notifiers.append(TelegramNotifier(token, cfg.telegram_chat_id))
    return notifiers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Шукає нові замовлення на Freelancehunt і складає чернетку відгуку."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--every", type=int, default=0,
        help="повторювати кожні N хвилин (0 — один прогін і вихід)",
    )
    parser.add_argument(
        "--no-draft", action="store_true", help="не складати чернетки відгуків"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    load_dotenv()

    try:
        cfg = Config.load(args.config)
        token = require(
            "FREELANCEHUNT_TOKEN",
            "Візьміть його в акаунті Freelancehunt, розділ «Додатки та API», "
            "і покладіть у .env.",
        )
        notifiers = build_notifiers(cfg)
    except ConfigError as exc:
        print(f"\n❌ {exc}\n", file=sys.stderr)
        return 2

    drafter = None
    if cfg.draft_replies and not args.no_draft:
        try:
            import anthropic

            key = require(
                "ANTHROPIC_API_KEY",
                "Він потрібен лише для чернеток відгуків. "
                "Без нього запускайте з ключем --no-draft.",
            )
            from monitor.drafter import Drafter

            drafter = Drafter(cfg, client=anthropic.Anthropic(api_key=key))
        except ConfigError as exc:
            print(f"\n❌ {exc}\n", file=sys.stderr)
            return 2

    errlog = ErrorLog(cfg.error_log)
    state = State(cfg.state_db)
    client = FreelancehuntClient(token, language=cfg.language)

    try:
        while True:
            report, _ = run_once(cfg, client, state, errlog, notifiers, drafter)
            print(report.render())
            if report.leads == 0:
                print("Нових підхожих замовлень немає.")
            if errlog.total:
                print(errlog.summary())
            if not args.every:
                break
            print(f"Наступна перевірка через {args.every} хв. Ctrl+C щоб зупинити.")
            time.sleep(args.every * 60)
    except KeyboardInterrupt:
        print("\nЗупинено.")
    finally:
        state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
