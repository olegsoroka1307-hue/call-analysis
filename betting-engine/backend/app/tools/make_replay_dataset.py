"""Генератор офлайн-датасету у форматі відповіді The Odds API v4.

ДАНІ СИНТЕТИЧНІ. Це не реальний ринок. Датасет існує лише тому, що зовнішні
odds-API недоступні з цього середовища (див. gate0/GATE0_REPORT.md), і потрібен
відтворюваний прогін PoC (ТЗ §52) без мережі.

Запуск:  python -m app.tools.make_replay_dataset [--days-ahead 3]
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

SPORT_KEY = "soccer_epl"
SPORT_TITLE = "EPL"
POLL_COUNT = 5  # ТЗ §52 п.4: 3-5 odds snapshots

BOOKMAKERS = [
    ("pinnacle", "Pinnacle"),   # sharp reference (ТЗ §0)
    ("stake", "Stake"),         # preferred bookmaker (ТЗ §33)
]

# (home, away, стартова тотальна лінія, лінія на останніх опитуваннях)
MATCHES = [
    ("Arsenal", "Tottenham Hotspur", 2.5, 2.5),
    ("Manchester City", "Brighton and Hove Albion", 3.5, 3.5),
    ("Liverpool", "Everton", 2.75, 2.75),
    ("Chelsea", "Newcastle United", 2.5, 2.75),   # рух самої лінії (ТЗ §28)
    ("Aston Villa", "Fulham", 2.25, 2.25),
]

# ТЗ §52 п.7 вимагає team O2.5, тому котируємо обидві типові командні лінії.
TEAM_TOTAL_LINES = (1.5, 2.5)


def _price_walk(rng: random.Random, start: float, steps: int, drift: float) -> list[float]:
    prices, price = [], start
    for _ in range(steps):
        price = round(price + drift + rng.uniform(-0.03, 0.03), 2)
        prices.append(max(1.05, price))
    return prices


def build(days_ahead: int, seed: int = 20260829) -> tuple[list[list[dict]], datetime]:
    rng = random.Random(seed)
    base = datetime.now(UTC).replace(second=0, microsecond=0)
    kickoff_base = base + timedelta(days=days_ahead)

    # Опитування відлічуються НАЗАД ВІД ЗАРАЗ, а не від kickoff.
    #
    # Раніше було `kickoff - offset`, і при days_ahead=3 навіть найраніший
    # зріз (T-48h) опинявся на добу В МАЙБУТНЬОМУ. Жоден odds-провайдер не
    # повертає last_update з майбутнього, тому такий датасет не міг би
    # приїхати з реального API — а PoC заявляє, що payload має точно ту саму
    # форму, що й відповідь The Odds API v4.
    #
    # Поки не було data-guard (аудит, п.3), це нічим не проявлялось. Тепер
    # від'ємний вік ціни коректно трактується як PROVIDER_ERROR, і весь
    # прогін чесно ставав PASS.
    #
    # Реалістична модель: систему опитували останні 48 годин, матч попереду.
    # Найсвіжіший зріз — 2 хвилини тому, щоб демо показувало живі рішення,
    # а не «все прострочене».
    offsets = [timedelta(hours=48), timedelta(hours=24), timedelta(hours=6),
               timedelta(hours=1), timedelta(minutes=2)][:POLL_COUNT]

    per_match = []
    for index, (home, away, line_start, line_end) in enumerate(MATCHES):
        kickoff = kickoff_base + timedelta(hours=index * 2)
        over_drift = -0.02 if index % 2 == 0 else 0.02   # steam vs drift (ТЗ §28)
        per_match.append({
            "event_id": f"poc{index:04d}{'ab3f' * 6}"[:32],
            "home": home, "away": away, "kickoff": kickoff,
            "lines": [line_start] * 3 + [line_end] * (POLL_COUNT - 3),
            "over": {
                "pinnacle": _price_walk(rng, 1.95 + index * 0.03, POLL_COUNT, over_drift),
                "stake": _price_walk(rng, 1.98 + index * 0.03, POLL_COUNT, over_drift),
            },
            "under": {
                "pinnacle": _price_walk(rng, 1.87 + index * 0.02, POLL_COUNT, -over_drift),
                "stake": _price_walk(rng, 1.90 + index * 0.02, POLL_COUNT, -over_drift),
            },
            "home_tt": {
                1.5: {
                    "over": _price_walk(rng, 2.05 + index * 0.04, POLL_COUNT, over_drift),
                    "under": _price_walk(rng, 1.74 + index * 0.03, POLL_COUNT, -over_drift),
                },
                2.5: {
                    "over": _price_walk(rng, 4.60 + index * 0.10, POLL_COUNT, over_drift * 3),
                    "under": _price_walk(rng, 1.18 + index * 0.01, POLL_COUNT, -over_drift * 0.3),
                },
            },
        })

    polls: list[list[dict]] = []
    for poll_index, offset in enumerate(offsets):
        events = []
        for match in per_match:
            observed_at = base - offset
            line = match["lines"][poll_index]
            bookmakers = []
            for key, title in BOOKMAKERS:
                stamp = observed_at.isoformat().replace("+00:00", "Z")
                bookmakers.append({
                    "key": key,
                    "title": title,
                    "last_update": stamp,
                    "markets": [
                        {
                            "key": "totals",
                            "last_update": stamp,
                            "outcomes": [
                                {"name": "Over", "price": match["over"][key][poll_index], "point": line},
                                {"name": "Under", "price": match["under"][key][poll_index], "point": line},
                            ],
                        },
                        {
                            "key": "team_totals",
                            "last_update": stamp,
                            "outcomes": [
                                outcome
                                for tt_line in TEAM_TOTAL_LINES
                                for outcome in (
                                    {"name": "Over", "description": match["home"],
                                     "price": match["home_tt"][tt_line]["over"][poll_index],
                                     "point": tt_line},
                                    {"name": "Under", "description": match["home"],
                                     "price": match["home_tt"][tt_line]["under"][poll_index],
                                     "point": tt_line},
                                )
                            ],
                        },
                    ],
                })
            events.append({
                "id": match["event_id"],
                "sport_key": SPORT_KEY,
                "sport_title": SPORT_TITLE,
                "commence_time": match["kickoff"].isoformat().replace("+00:00", "Z"),
                "home_team": match["home"],
                "away_team": match["away"],
                "bookmakers": bookmakers,
            })
        polls.append(events)
    return polls, base


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days-ahead", type=int, default=3)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "data"))
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    polls, generated_at = build(args.days_ahead)

    filenames = []
    for index, events in enumerate(polls):
        name = f"odds_{SPORT_KEY}_t{index}.json"
        (out_dir / name).write_text(json.dumps(events, indent=2, ensure_ascii=False), encoding="utf-8")
        filenames.append(name)

    manifest = {
        "data_source": "SYNTHETIC_REPLAY",
        "warning": (
            "СИНТЕТИЧНІ ДАНІ. Не реальні коефіцієнти. Згенеровано, бо Gate 0 "
            "не пройдений і немає API-ключів — див. gate0/GATE0_REPORT.md."
        ),
        "payload_format": "the-odds-api-v4",
        "sport_key": SPORT_KEY,
        "generated_at": generated_at.isoformat(),
        "poll_schedule": ["-48h", "-24h", "-6h", "-1h", "-2m"][:len(polls)],
        "poll_schedule_note": (
            "Зсуви відлічені назад від моменту генерації. Усі зрізи в минулому "
            "і до kickoff — як у справжнього провайдера."
        ),
        "events": len(polls[0]),
        "polls": filenames,
    }
    (out_dir / "replay_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"wrote {len(filenames)} polls x {len(polls[0])} events -> {out_dir}")


if __name__ == "__main__":
    main()
