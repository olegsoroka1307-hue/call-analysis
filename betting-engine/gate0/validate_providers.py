"""Gate 0 — валідація data providers (ТЗ §3).

ТЗ §3 вимагає ДО початку розробки:
    1. Взяти 20-30 реальних матчів з потрібних ліг.
    2. Протестувати мінімум 2-3 data providers.
    3. Для кожного перевірити, чи реально повертаються потрібні markets і timestamps.
    4. Показати короткий звіт до початку інтеграції.

Цей скрипт робить саме це і друкує таблицю у форматі §3.

Запуск:
    python gate0/validate_providers.py --preflight-only
    THE_ODDS_API_KEY=... API_FOOTBALL_KEY=... python gate0/validate_providers.py

Без ключів або без мережі скрипт НЕ вигадує результати: він позначає клітинки
UNKNOWN і в підсумку каже, що Gate 0 не пройдений (ТЗ §1).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

try:
    import httpx
except ImportError:  # pragma: no cover
    print("потрібен httpx:  pip install httpx", file=sys.stderr)
    raise

TARGET_MATCH_COUNT = 25          # ТЗ §3: 20-30 матчів
TARGET_LEAGUES = ["soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga"]

#: Стовпці таблиці приймання з ТЗ §3.
CAPABILITIES = [
    "Fixtures", "Stake odds", "Asian totals", "Team totals", "BTTS",
    "Historical odds", "xG", "Lineups", "Injuries", "Price/limits",
]

#: Ринки, без яких Gate 0 не проходиться (ТЗ §3, §8).
GATE0_REQUIRED = ["Fixtures", "Asian totals", "Team totals", "BTTS", "Historical odds"]


class Status:
    YES = "YES"
    NO = "NO"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"      # не перевірено — не плутати з NO
    BLOCKED = "BLOCKED"      # хост недоступний з цього середовища
    NO_KEY = "NO_KEY"        # немає API-ключа


@dataclass
class ProviderSpec:
    name: str
    host: str
    base_url: str
    key_env: str
    docs: str
    probe: Callable[["ProviderResult", str], None] | None = None
    notes: str = ""


@dataclass
class ProviderResult:
    name: str
    host: str
    reachable: str = Status.UNKNOWN
    reachability_detail: str = ""
    has_key: bool = False
    capabilities: dict[str, str] = field(default_factory=dict)
    matches_checked: int = 0
    sample_timestamps: list[str] = field(default_factory=list)
    latency_ms: float | None = None
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for capability in CAPABILITIES:
            self.capabilities.setdefault(capability, Status.UNKNOWN)

    def mark_all(self, status: str) -> None:
        for capability in CAPABILITIES:
            self.capabilities[capability] = status

    @property
    def gate0_passed(self) -> bool:
        return all(self.capabilities.get(c) == Status.YES for c in GATE0_REQUIRED)


# --------------------------------------------------------------------------
# Preflight: чи взагалі досяжний хост
# --------------------------------------------------------------------------

def check_reachability(host: str, port: int = 443, timeout: float = 10.0) -> tuple[str, str]:
    """Перевірка мережі до конкретного провайдера.

    Розрізняє «хост не існує», «заблоковано політикою egress» і «працює» —
    це різні висновки для Gate 0.
    """
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if proxy:
        try:
            response = httpx.get(f"https://{host}/", timeout=timeout, follow_redirects=False)
            return Status.YES, f"HTTP {response.status_code} через проксі"
        except httpx.ProxyError as exc:
            return Status.BLOCKED, f"проксі відхилив CONNECT: {exc}"
        except httpx.ConnectError as exc:
            detail = str(exc)
            if "403" in detail or "407" in detail:
                return Status.BLOCKED, f"egress-політика заборонила хост: {detail}"
            return Status.NO, f"немає з'єднання: {detail}"
        except httpx.HTTPError as exc:
            return Status.NO, f"помилка транспорту: {exc}"

    try:
        socket.getaddrinfo(host, port)
    except socket.gaierror as exc:
        return Status.NO, f"DNS не резолвиться: {exc}"
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host):
                return Status.YES, "TLS-з'єднання встановлено"
    except (OSError, ssl.SSLError) as exc:
        return Status.NO, f"з'єднання не вдалося: {exc}"


# --------------------------------------------------------------------------
# Проби конкретних провайдерів
# --------------------------------------------------------------------------

def _get(url: str, params: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
    response = httpx.get(url, params=params, headers=headers or {}, timeout=30.0)
    response.raise_for_status()
    return response.json()


def probe_the_odds_api(result: ProviderResult, api_key: str) -> None:
    """The Odds API v4: коефіцієнти, asian totals, team totals, historical."""
    base = "https://api.the-odds-api.com/v4"
    started = time.monotonic()
    events: list[dict] = []
    for league in TARGET_LEAGUES:
        if len(events) >= TARGET_MATCH_COUNT:
            break
        try:
            events += _get(
                f"{base}/sports/{league}/odds",
                {"apiKey": api_key, "regions": "eu",
                 "markets": "totals,spreads", "oddsFormat": "decimal"},
            )
        except httpx.HTTPError as exc:
            result.errors.append(f"{league}: {exc}")
    result.latency_ms = (time.monotonic() - started) * 1000
    events = events[:TARGET_MATCH_COUNT]
    result.matches_checked = len(events)

    if not events:
        result.mark_all(Status.NO)
        result.capabilities["Fixtures"] = Status.NO
        return

    result.capabilities["Fixtures"] = Status.YES

    bookmaker_keys, total_lines, has_team_totals, has_btts, stamps = set(), set(), False, False, []
    for event in events:
        for bookmaker in event.get("bookmakers", []):
            bookmaker_keys.add(bookmaker["key"])
            if bookmaker.get("last_update"):
                stamps.append(bookmaker["last_update"])
            for market in bookmaker.get("markets", []):
                if market["key"] == "totals":
                    total_lines.update(
                        o.get("point") for o in market.get("outcomes", []) if o.get("point")
                    )
                has_team_totals |= market["key"] == "team_totals"
                has_btts |= market["key"] == "btts"

    result.sample_timestamps = sorted(set(stamps))[:3]
    # Asian totals = наявність чвертьових ліній (x.25 / x.75) — ТЗ §8.1.
    quarters = [l for l in total_lines if round(l * 4) % 2 == 1]
    result.capabilities["Asian totals"] = Status.YES if quarters else Status.PARTIAL
    result.capabilities["Team totals"] = Status.YES if has_team_totals else Status.NO
    result.capabilities["BTTS"] = Status.YES if has_btts else Status.NO
    result.capabilities["Stake odds"] = (
        Status.YES if any("stake" in k for k in bookmaker_keys) else Status.NO
    )
    result.capabilities["Price/limits"] = Status.NO  # публічний API не віддає ліміти

    try:
        historical = _get(
            f"{base}/historical/sports/{TARGET_LEAGUES[0]}/odds",
            {"apiKey": api_key, "regions": "eu", "markets": "totals",
             "oddsFormat": "decimal",
             "date": (datetime.now(UTC) - timedelta(days=7)).isoformat()},
        )
        result.capabilities["Historical odds"] = (
            Status.YES if historical.get("data") else Status.NO
        )
    except httpx.HTTPError as exc:
        result.capabilities["Historical odds"] = Status.NO
        result.errors.append(f"historical: {exc}")

    # Статистичні поля цей провайдер не віддає в принципі.
    for capability in ("xG", "Lineups", "Injuries"):
        result.capabilities[capability] = Status.NO


def probe_api_football(result: ProviderResult, api_key: str) -> None:
    """API-Football (api-sports.io): fixtures, статистика, склади, травми, odds."""
    base = "https://v3.football.api-sports.io"
    headers = {"x-apisports-key": api_key}
    started = time.monotonic()
    try:
        payload = _get(f"{base}/fixtures", {"league": 39, "season": 2025, "next": TARGET_MATCH_COUNT}, headers)
    except httpx.HTTPError as exc:
        result.mark_all(Status.NO)
        result.errors.append(f"fixtures: {exc}")
        return
    result.latency_ms = (time.monotonic() - started) * 1000

    fixtures = payload.get("response", [])
    result.matches_checked = len(fixtures)
    result.capabilities["Fixtures"] = Status.YES if fixtures else Status.NO
    if not fixtures:
        return
    result.sample_timestamps = [
        f["fixture"]["date"] for f in fixtures[:3] if f.get("fixture", {}).get("date")
    ]

    fixture_id = fixtures[0]["fixture"]["id"]
    checks = {
        "Lineups": (f"{base}/fixtures/lineups", {"fixture": fixture_id}),
        "Injuries": (f"{base}/injuries", {"fixture": fixture_id}),
        "xG": (f"{base}/fixtures/statistics", {"fixture": fixture_id}),
    }
    for capability, (url, params) in checks.items():
        try:
            response = _get(url, params, headers)
            result.capabilities[capability] = (
                Status.YES if response.get("response") else Status.PARTIAL
            )
        except httpx.HTTPError as exc:
            result.capabilities[capability] = Status.NO
            result.errors.append(f"{capability}: {exc}")

    try:
        odds = _get(f"{base}/odds", {"fixture": fixture_id}, headers)
        markets, bookmakers = set(), set()
        for entry in odds.get("response", []):
            for bookmaker in entry.get("bookmakers", []):
                bookmakers.add(bookmaker.get("name", "").lower())
                for bet in bookmaker.get("bets", []):
                    markets.add(bet.get("name", ""))
        result.capabilities["Asian totals"] = (
            Status.YES if any("asian" in m.lower() for m in markets) else Status.PARTIAL
        )
        result.capabilities["Team totals"] = (
            Status.YES if any("team total" in m.lower() for m in markets) else Status.NO
        )
        result.capabilities["BTTS"] = (
            Status.YES if any("both teams" in m.lower() for m in markets) else Status.NO
        )
        result.capabilities["Stake odds"] = (
            Status.YES if any("stake" in b for b in bookmakers) else Status.NO
        )
    except httpx.HTTPError as exc:
        result.errors.append(f"odds: {exc}")

    result.capabilities["Historical odds"] = Status.PARTIAL  # лише за fixture, без снапшотів руху
    result.capabilities["Price/limits"] = Status.NO


PROVIDERS = [
    ProviderSpec(
        name="The Odds API v4",
        host="api.the-odds-api.com",
        base_url="https://api.the-odds-api.com/v4",
        key_env="THE_ODDS_API_KEY",
        docs="https://the-odds-api.com/liveapi/guides/v4/",
        probe=probe_the_odds_api,
        notes="Сильний по odds і historical odds; не має xG/lineups/injuries.",
    ),
    ProviderSpec(
        name="API-Football (api-sports.io)",
        host="v3.football.api-sports.io",
        base_url="https://v3.football.api-sports.io",
        key_env="API_FOOTBALL_KEY",
        docs="https://www.api-football.com/documentation-v3",
        probe=probe_api_football,
        notes="Сильний по статистиці/складах/травмах; historical odds — без руху лінії.",
    ),
    ProviderSpec(
        name="SportMonks Football",
        host="api.sportmonks.com",
        base_url="https://api.sportmonks.com/v3/football",
        key_env="SPORTMONKS_API_KEY",
        docs="https://docs.sportmonks.com/football",
        probe=None,
        notes="Проба не реалізована — додати після отримання тестового доступу.",
    ),
]


# --------------------------------------------------------------------------
# Звіт
# --------------------------------------------------------------------------

def render_table(results: list[ProviderResult]) -> str:
    header = "| Provider | " + " | ".join(CAPABILITIES) + " |"
    divider = "|" + "---|" * (len(CAPABILITIES) + 1)
    rows = [
        "| " + result.name + " | "
        + " | ".join(result.capabilities[c] for c in CAPABILITIES) + " |"
        for result in results
    ]
    return "\n".join([header, divider, *rows])


def render_report(results: list[ProviderResult]) -> str:
    lines = [
        "# Gate 0 — Provider validation (ТЗ §3)",
        "",
        f"Згенеровано: {datetime.now(UTC).isoformat()}",
        f"Ціль: {TARGET_MATCH_COUNT} матчів, ліги: {', '.join(TARGET_LEAGUES)}",
        "",
        "## Матриця покриття",
        "",
        render_table(results),
        "",
        "## Деталі по провайдерах",
        "",
    ]
    for result in results:
        lines += [
            f"### {result.name}",
            "",
            f"- Хост: `{result.host}`",
            f"- Досяжність: **{result.reachable}** — {result.reachability_detail}",
            f"- API-ключ: {'є' if result.has_key else 'НЕМАЄ'}",
            f"- Матчів перевірено: {result.matches_checked}",
            f"- Затримка: {result.latency_ms:.0f} ms" if result.latency_ms else "- Затримка: n/a",
            f"- Приклади timestamps: {result.sample_timestamps or 'n/a'}",
            f"- Gate 0: {'ПРОЙДЕНО' if result.gate0_passed else 'НЕ ПРОЙДЕНО'}",
        ]
        if result.errors:
            lines.append(f"- Помилки: {result.errors[:5]}")
        lines.append("")

    passed = [r for r in results if r.gate0_passed]
    lines += [
        "## Висновок",
        "",
        (
            f"Gate 0 ПРОЙДЕНО. Провайдери, що покривають ключові ринки: "
            f"{', '.join(r.name for r in passed)}."
            if passed else
            "Gate 0 НЕ ПРОЙДЕНО. Жоден провайдер не підтвердив покриття ключових "
            f"ринків ({', '.join(GATE0_REQUIRED)}). Переходити до Sprint 1 не можна (ТЗ §48)."
        ),
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-only", action="store_true",
                        help="лише перевірка мережевої досяжності, без API-запитів")
    parser.add_argument("--json-out", help="куди зберегти сирий результат у JSON")
    parser.add_argument("--md-out", help="куди зберегти звіт у Markdown")
    args = parser.parse_args()

    results: list[ProviderResult] = []
    for spec in PROVIDERS:
        result = ProviderResult(name=spec.name, host=spec.host)
        result.reachable, result.reachability_detail = check_reachability(spec.host)
        api_key = os.getenv(spec.key_env, "")
        result.has_key = bool(api_key)

        print(f"[{result.reachable:>7}] {spec.name:<32} {spec.host}")
        print(f"          {result.reachability_detail}")

        if result.reachable == Status.BLOCKED:
            result.mark_all(Status.BLOCKED)
        elif result.reachable != Status.YES:
            result.mark_all(Status.UNKNOWN)
        elif not api_key:
            result.mark_all(Status.NO_KEY)
            print(f"          немає ключа — задайте {spec.key_env}")
        elif spec.probe is None:
            result.mark_all(Status.UNKNOWN)
            print("          проба не реалізована")
        elif not args.preflight_only:
            spec.probe(result, api_key)

        results.append(result)

    report = render_report(results)
    print("\n" + report)

    if args.md_out:
        with open(args.md_out, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(
                [
                    {
                        "name": r.name, "host": r.host, "reachable": r.reachable,
                        "detail": r.reachability_detail, "has_key": r.has_key,
                        "capabilities": r.capabilities, "matches_checked": r.matches_checked,
                        "errors": r.errors, "gate0_passed": r.gate0_passed,
                    }
                    for r in results
                ],
                handle, indent=2, ensure_ascii=False,
            )

    return 0 if any(r.gate0_passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
