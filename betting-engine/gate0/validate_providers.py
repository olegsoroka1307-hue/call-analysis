"""Gate 0 — валідація data providers (ТЗ §3).

ТЗ §3 вимагає ДО початку розробки:
    1. Взяти 20-30 реальних матчів з потрібних ліг.
    2. Протестувати мінімум 2-3 data providers.
    3. Для кожного перевірити, чи реально повертаються потрібні markets і timestamps.
    4. Показати короткий звіт до початку інтеграції.

Запуск:
    python gate0/validate_providers.py --preflight-only
    THE_ODDS_API_KEY=... API_FOOTBALL_KEY=... SPORTMONKS_API_KEY=... \
        python gate0/validate_providers.py --matches 25

Без ключів або без мережі скрипт НЕ вигадує результати: він позначає клітинки
UNKNOWN і в підсумку каже, що Gate 0 не пройдений (ТЗ §1).

Що тут принципово (виправлено за аудитом 29.08.2026):

* **Перевіряється те, що запитано.** Раніше запит ішов з `markets=totals,spreads`,
  а результат читав `team_totals` і `btts` — їх у відповіді не могло бути в
  принципі, тому обидва ринки завжди виходили NO. Це не «провайдер не вміє»,
  це помилка проби. Додаткові ринки The Odds API живуть на окремому
  event-endpoint, туди й запитуємо.
* **Покриття рахується на 20-30 матчах, а не на одному.** Один fixture не
  говорить нічого про ринок: у конкретного матчу може просто не бути ліній.
  Тому статус ринку — це частка матчів, де він реально знайшовся.
* **xG не «є, бо відповідь непорожня».** `/fixtures/statistics` повертає
  непорожній масив і без xG. Шукаємо саме тип `expected_goals`.
* **Historical odds не виставляються PARTIAL наздогад** — тільки живим запитом.
* **Статистика входить у формальний гейт.** Модель без xG/складів/травм — це
  вже не той двигун, що описаний у ТЗ. Гейт рахується по стеку: кожна
  потрібна здатність має бути хоч в одного провайдера.
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


def _force_utf8_output() -> None:
    """Windows-консоль за замовчуванням не cp65001.

    Без цього звіт з кирилицею падає на UnicodeEncodeError, і запуск
    доводилось обкладати PYTHONUTF8=1. Скрипт має запускатись командою
    з README, а не спеціальним заклинанням.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass


_force_utf8_output()

DEFAULT_MATCH_COUNT = 25         # ТЗ §3: 20-30 матчів
MIN_MATCH_COUNT = 20
TARGET_LEAGUES = ["soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga"]

#: Частка матчів, з якої ринок вважається реально доступним, а не випадковим.
COVERAGE_YES = 0.80

#: Скільки матчів опитувати по «дорогих» per-event endpoint'ах.
EVENT_PROBE_LIMIT = 25

#: Стовпці таблиці приймання з ТЗ §3.
CAPABILITIES = [
    "Fixtures", "Stake odds", "Asian totals", "Team totals", "BTTS",
    "Historical odds", "xG", "Lineups", "Injuries", "Price/limits",
]

#: Ринки, без яких Gate 0 не проходиться (ТЗ §3, §8).
GATE0_REQUIRED_ODDS = ["Fixtures", "Asian totals", "Team totals", "BTTS", "Historical odds"]
#: Статистика — теж умова гейта: без неї lambda з ТЗ §9-§11 нема з чого рахувати.
GATE0_REQUIRED_STATS = ["xG", "Lineups", "Injuries"]
GATE0_REQUIRED = GATE0_REQUIRED_ODDS + GATE0_REQUIRED_STATS


class Status:
    YES = "YES"
    NO = "NO"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"      # не перевірено — не плутати з NO
    BLOCKED = "BLOCKED"      # хост недоступний з цього середовища
    NO_KEY = "NO_KEY"        # немає API-ключа


def coverage_status(covered: int, checked: int) -> str:
    """Статус ринку за покриттям, а не за одним вдалим випадком."""
    if checked <= 0:
        return Status.UNKNOWN
    ratio = covered / checked
    if ratio >= COVERAGE_YES:
        return Status.YES
    if covered > 0:
        return Status.PARTIAL
    return Status.NO


@dataclass
class ProviderSpec:
    name: str
    host: str
    base_url: str
    key_env: str
    docs: str
    probe: Callable[["ProviderResult", str, int], None] | None = None
    notes: str = ""


@dataclass
class ProviderResult:
    name: str
    host: str
    reachable: str = Status.UNKNOWN
    reachability_detail: str = ""
    has_key: bool = False
    capabilities: dict[str, str] = field(default_factory=dict)
    #: capability -> {"covered": n, "checked": m} — чим підкріплений статус.
    coverage: dict[str, dict[str, int]] = field(default_factory=dict)
    matches_checked: int = 0
    events_probed: int = 0
    sample_timestamps: list[str] = field(default_factory=list)
    latency_ms: float | None = None
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for capability in CAPABILITIES:
            self.capabilities.setdefault(capability, Status.UNKNOWN)

    def mark_all(self, status: str) -> None:
        for capability in CAPABILITIES:
            self.capabilities[capability] = status

    def set_coverage(self, capability: str, covered: int, checked: int) -> None:
        self.coverage[capability] = {"covered": covered, "checked": checked}
        self.capabilities[capability] = coverage_status(covered, checked)

    @property
    def sample_is_sufficient(self) -> bool:
        """ТЗ §3 говорить про 20-30 матчів. Менше — вибірка не рахується."""
        return self.matches_checked >= MIN_MATCH_COUNT


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


def _is_quarter_line(value: Any) -> bool:
    """Чвертькова лінія (x.25 / x.75) — ознака справжніх asian totals (ТЗ §8.1)."""
    try:
        return round(float(value) * 4) % 2 == 1
    except (TypeError, ValueError):
        return False


def probe_the_odds_api(result: ProviderResult, api_key: str, match_count: int) -> None:
    """The Odds API v4.

    Core markets (`totals`, `spreads`) приходять зі спортового endpoint'а.
    Додаткові (`team_totals`, `btts`, `alternate_totals`) в v4 доступні лише
    per-event, тому їх опитуємо окремо — інакше перевірка їхньої наявності
    не має сенсу.
    """
    base = "https://api.the-odds-api.com/v4"
    started = time.monotonic()
    events: list[dict] = []
    for league in TARGET_LEAGUES:
        if len(events) >= match_count:
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
    events = events[:match_count]
    result.matches_checked = len(events)

    if not events:
        result.mark_all(Status.NO)
        result.capabilities["Fixtures"] = Status.NO
        return

    result.set_coverage("Fixtures", len(events), len(events))

    stamps: list[str] = []
    asian_hits = 0
    stake_hits = 0
    for event in events:
        event_has_quarter = False
        event_has_stake = False
        for bookmaker in event.get("bookmakers", []):
            if "stake" in bookmaker.get("key", ""):
                event_has_stake = True
            if bookmaker.get("last_update"):
                stamps.append(bookmaker["last_update"])
            for market in bookmaker.get("markets", []):
                if market.get("key") == "totals":
                    if any(_is_quarter_line(o.get("point")) for o in market.get("outcomes", [])):
                        event_has_quarter = True
        asian_hits += int(event_has_quarter)
        stake_hits += int(event_has_stake)

    result.sample_timestamps = sorted(set(stamps))[:3]
    result.set_coverage("Asian totals", asian_hits, len(events))
    result.set_coverage("Stake odds", stake_hits, len(events))

    # --- додаткові ринки: тільки per-event endpoint ------------------------
    sport_of: dict[str, str] = {}
    for event in events:
        if event.get("id") and event.get("sport_key"):
            sport_of[event["id"]] = event["sport_key"]

    probe_ids = list(sport_of)[:EVENT_PROBE_LIMIT]
    team_totals_hits = 0
    btts_hits = 0
    probed = 0
    for event_id in probe_ids:
        try:
            payload = _get(
                f"{base}/sports/{sport_of[event_id]}/events/{event_id}/odds",
                {"apiKey": api_key, "regions": "eu",
                 "markets": "team_totals,btts", "oddsFormat": "decimal"},
            )
        except httpx.HTTPError as exc:
            result.errors.append(f"event {event_id} additional markets: {exc}")
            continue
        probed += 1
        keys = {
            market.get("key")
            for bookmaker in payload.get("bookmakers", [])
            for market in bookmaker.get("markets", [])
        }
        team_totals_hits += int("team_totals" in keys)
        btts_hits += int("btts" in keys)

    result.events_probed = probed
    if probed:
        result.set_coverage("Team totals", team_totals_hits, probed)
        result.set_coverage("BTTS", btts_hits, probed)
    else:
        # Жодного успішного per-event запиту — про ці ринки не відомо нічого.
        result.capabilities["Team totals"] = Status.UNKNOWN
        result.capabilities["BTTS"] = Status.UNKNOWN

    result.capabilities["Price/limits"] = Status.NO  # публічний API не віддає ліміти

    # --- historical: тільки живим запитом ----------------------------------
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
        # 401/403 тут = «тариф не включає historical», а не «немає такого».
        result.capabilities["Historical odds"] = Status.NO
        result.errors.append(f"historical: {exc}")

    # Статистичні поля цей провайдер не віддає в принципі.
    for capability in ("xG", "Lineups", "Injuries"):
        result.capabilities[capability] = Status.NO


def _api_football_has_xg(statistics_payload: dict) -> bool:
    """xG підтверджений лише наявністю типу expected_goals.

    Непорожній `/fixtures/statistics` сам по собі нічого не доводить: там
    можуть бути удари, володіння і кутові — і жодного xG.
    """
    for team_block in statistics_payload.get("response", []):
        for statistic in team_block.get("statistics", []):
            name = str(statistic.get("type", "")).lower().replace("_", " ")
            if "expected goals" in name or name == "xg":
                if statistic.get("value") not in (None, ""):
                    return True
    return False


def probe_api_football(result: ProviderResult, api_key: str, match_count: int) -> None:
    """API-Football (api-sports.io): fixtures, статистика, склади, травми, odds."""
    base = "https://v3.football.api-sports.io"
    headers = {"x-apisports-key": api_key}
    started = time.monotonic()
    try:
        upcoming = _get(
            f"{base}/fixtures",
            {"league": 39, "season": 2025, "next": match_count},
            headers,
        )
    except httpx.HTTPError as exc:
        result.mark_all(Status.NO)
        result.errors.append(f"fixtures: {exc}")
        return
    result.latency_ms = (time.monotonic() - started) * 1000

    fixtures = upcoming.get("response", [])
    result.matches_checked = len(fixtures)
    result.set_coverage("Fixtures", len(fixtures), max(len(fixtures), 1))
    if not fixtures:
        result.capabilities["Fixtures"] = Status.NO
        return
    result.sample_timestamps = [
        f["fixture"]["date"] for f in fixtures[:3] if f.get("fixture", {}).get("date")
    ]

    # xG рахуємо на ЗІГРАНИХ матчах: у майбутнього матчу статистики немає за
    # визначенням, і перевірка на ньому нічого не означала б.
    played: list[dict] = []
    try:
        played = _get(
            f"{base}/fixtures",
            {"league": 39, "season": 2025, "last": match_count},
            headers,
        ).get("response", [])
    except httpx.HTTPError as exc:
        result.errors.append(f"fixtures(last): {exc}")

    xg_hits = 0
    xg_checked = 0
    for fixture in played[:EVENT_PROBE_LIMIT]:
        fixture_id = fixture.get("fixture", {}).get("id")
        if fixture_id is None:
            continue
        try:
            statistics = _get(f"{base}/fixtures/statistics", {"fixture": fixture_id}, headers)
        except httpx.HTTPError as exc:
            result.errors.append(f"xG fixture {fixture_id}: {exc}")
            continue
        xg_checked += 1
        xg_hits += int(_api_football_has_xg(statistics))
    if xg_checked:
        result.set_coverage("xG", xg_hits, xg_checked)
    else:
        result.capabilities["xG"] = Status.UNKNOWN

    # Склади і травми — на найближчих матчах, теж по вибірці.
    lineup_hits = injury_hits = probed = 0
    for fixture in fixtures[:EVENT_PROBE_LIMIT]:
        fixture_id = fixture.get("fixture", {}).get("id")
        if fixture_id is None:
            continue
        probed += 1
        for capability, url in (
            ("Lineups", f"{base}/fixtures/lineups"),
            ("Injuries", f"{base}/injuries"),
        ):
            try:
                payload = _get(url, {"fixture": fixture_id}, headers)
            except httpx.HTTPError as exc:
                result.errors.append(f"{capability} fixture {fixture_id}: {exc}")
                continue
            if payload.get("response"):
                if capability == "Lineups":
                    lineup_hits += 1
                else:
                    injury_hits += 1
    result.events_probed = probed
    if probed:
        result.set_coverage("Lineups", lineup_hits, probed)
        result.set_coverage("Injuries", injury_hits, probed)

    # Ринки — теж по вибірці матчів, а не по першому.
    asian_hits = team_total_hits = btts_hits = stake_hits = odds_probed = 0
    for fixture in fixtures[:EVENT_PROBE_LIMIT]:
        fixture_id = fixture.get("fixture", {}).get("id")
        if fixture_id is None:
            continue
        try:
            odds = _get(f"{base}/odds", {"fixture": fixture_id}, headers)
        except httpx.HTTPError as exc:
            result.errors.append(f"odds fixture {fixture_id}: {exc}")
            continue
        odds_probed += 1
        markets, bookmakers = set(), set()
        for entry in odds.get("response", []):
            for bookmaker in entry.get("bookmakers", []):
                bookmakers.add(str(bookmaker.get("name", "")).lower())
                for bet in bookmaker.get("bets", []):
                    markets.add(str(bet.get("name", "")).lower())
        asian_hits += int(any("asian" in m for m in markets))
        team_total_hits += int(any("team total" in m for m in markets))
        btts_hits += int(any("both teams" in m for m in markets))
        stake_hits += int(any("stake" in b for b in bookmakers))

    if odds_probed:
        result.set_coverage("Asian totals", asian_hits, odds_probed)
        result.set_coverage("Team totals", team_total_hits, odds_probed)
        result.set_coverage("BTTS", btts_hits, odds_probed)
        result.set_coverage("Stake odds", stake_hits, odds_probed)

    # Historical odds — живий запит по зіграному матчу, без здогадок.
    if played:
        past_id = played[0].get("fixture", {}).get("id")
        try:
            historical = _get(f"{base}/odds", {"fixture": past_id}, headers)
            result.capabilities["Historical odds"] = (
                Status.YES if historical.get("response") else Status.NO
            )
        except httpx.HTTPError as exc:
            result.capabilities["Historical odds"] = Status.NO
            result.errors.append(f"historical odds: {exc}")
    else:
        result.capabilities["Historical odds"] = Status.UNKNOWN

    result.capabilities["Price/limits"] = Status.NO


def _walk_strings(node: Any) -> list[str]:
    """Усі рядкові значення в довільному JSON — для пошуку назв ринків."""
    found: list[str] = []
    if isinstance(node, dict):
        for value in node.values():
            found += _walk_strings(value)
    elif isinstance(node, list):
        for value in node:
            found += _walk_strings(value)
    elif isinstance(node, str):
        found.append(node.lower())
    return found


def probe_sportmonks(result: ProviderResult, api_key: str, match_count: int) -> None:
    """SportMonks Football v3.

    Проба свідомо консервативна: якщо форма відповіді не та, на яку
    розраховано, статус лишається UNKNOWN з поміткою помилки. Заповнити
    таблицю здогадкою було б гірше, ніж не заповнити (ТЗ §1).
    """
    base = "https://api.sportmonks.com/v3/football"
    params = {"api_token": api_key}
    started = time.monotonic()

    try:
        payload = _get(f"{base}/fixtures", {**params, "per_page": match_count})
    except httpx.HTTPError as exc:
        result.mark_all(Status.NO)
        result.errors.append(f"fixtures: {exc}")
        return
    result.latency_ms = (time.monotonic() - started) * 1000

    fixtures = payload.get("data") or []
    result.matches_checked = len(fixtures)
    if not fixtures:
        result.capabilities["Fixtures"] = Status.NO
        result.errors.append("fixtures: відповідь без поля data")
        return
    result.set_coverage("Fixtures", len(fixtures), len(fixtures))
    result.sample_timestamps = [
        str(f.get("starting_at")) for f in fixtures[:3] if f.get("starting_at")
    ]

    asian_hits = team_total_hits = btts_hits = stake_hits = quarter_hits = probed = 0
    for fixture in fixtures[:EVENT_PROBE_LIMIT]:
        fixture_id = fixture.get("id")
        if fixture_id is None:
            continue
        try:
            odds = _get(f"{base}/odds/pre-match/fixtures/{fixture_id}", params)
        except httpx.HTTPError as exc:
            result.errors.append(f"odds fixture {fixture_id}: {exc}")
            continue
        probed += 1
        rows = odds.get("data") or []
        text = set(_walk_strings(rows))
        asian_hits += int(any("asian" in t for t in text))
        team_total_hits += int(any("team total" in t for t in text))
        btts_hits += int(any("both teams" in t for t in text))
        stake_hits += int(any("stake" in t for t in text))
        quarter_hits += int(any(_is_quarter_line(row.get("total")) for row in rows
                                if isinstance(row, dict)))

    result.events_probed = probed
    if probed:
        # Asian totals підтверджуємо або назвою ринку, або чвертьковою лінією.
        result.set_coverage("Asian totals", max(asian_hits, quarter_hits), probed)
        result.set_coverage("Team totals", team_total_hits, probed)
        result.set_coverage("BTTS", btts_hits, probed)
        result.set_coverage("Stake odds", stake_hits, probed)

    # Статистика і xG — через include на зіграних матчах.
    try:
        stats = _get(
            f"{base}/fixtures",
            {**params, "per_page": 5, "include": "statistics.type", "filters": "fixtureStates:5"},
        )
        text = set(_walk_strings(stats.get("data") or []))
        result.capabilities["xG"] = (
            Status.YES if any("expected goal" in t or t == "xg" for t in text) else Status.NO
        )
    except httpx.HTTPError as exc:
        result.errors.append(f"statistics: {exc}")

    for capability, include in (("Lineups", "lineups"), ("Injuries", "sidelined")):
        try:
            payload = _get(f"{base}/fixtures", {**params, "per_page": 5, "include": include})
            rows = payload.get("data") or []
            hits = sum(1 for row in rows if isinstance(row, dict) and row.get(include))
            result.set_coverage(capability, hits, len(rows) or 1)
        except httpx.HTTPError as exc:
            result.errors.append(f"{capability}: {exc}")

    # Historical odds: у SportMonks це окремий продукт — перевіряємо живим запитом.
    try:
        historical = _get(f"{base}/odds/pre-match/latest", params)
        result.capabilities["Historical odds"] = (
            Status.YES if historical.get("data") else Status.NO
        )
    except httpx.HTTPError as exc:
        result.capabilities["Historical odds"] = Status.NO
        result.errors.append(f"historical: {exc}")

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
        probe=probe_sportmonks,
        notes="Проба консервативна: незнайома форма відповіді лишає UNKNOWN, не здогадку.",
    ),
]


# --------------------------------------------------------------------------
# Підсумок по стеку
# --------------------------------------------------------------------------

def stack_coverage(results: list[ProviderResult]) -> dict[str, str]:
    """Гейт рахується по стеку: здатність закрита, якщо її має хоч один провайдер.

    ТЗ §3 просить перевірити 2-3 провайдери саме тому, що жоден поодинці
    не закриває і ринки, і статистику.
    """
    summary: dict[str, str] = {}
    for capability in GATE0_REQUIRED:
        statuses = [r.capabilities.get(capability, Status.UNKNOWN) for r in results]
        if Status.YES in statuses:
            summary[capability] = Status.YES
        elif Status.PARTIAL in statuses:
            summary[capability] = Status.PARTIAL
        elif all(s in (Status.UNKNOWN, Status.BLOCKED, Status.NO_KEY) for s in statuses):
            summary[capability] = Status.UNKNOWN
        else:
            summary[capability] = Status.NO
    return summary


def gate0_passed(results: list[ProviderResult]) -> tuple[bool, list[str]]:
    """Чи пройдений Gate 0 і, якщо ні, — що саме заважає."""
    blockers: list[str] = []
    summary = stack_coverage(results)
    for capability, status in summary.items():
        if status != Status.YES:
            blockers.append(f"{capability}: {status}")

    checked = [r for r in results if r.matches_checked]
    if not checked:
        blockers.append("жоден провайдер не опитаний на реальних матчах")
    else:
        thin = [r.name for r in checked if not r.sample_is_sufficient]
        if thin:
            blockers.append(
                f"вибірка менша за {MIN_MATCH_COUNT} матчів: {', '.join(thin)}"
            )
    return (not blockers), blockers


# --------------------------------------------------------------------------
# Звіт
# --------------------------------------------------------------------------

def render_table(results: list[ProviderResult]) -> str:
    header = "| Provider | " + " | ".join(CAPABILITIES) + " |"
    divider = "|" + "---|" * (len(CAPABILITIES) + 1)
    rows = [
        "| " + result.name + " | "
        + " | ".join(result.capabilities.get(c, Status.UNKNOWN) for c in CAPABILITIES)
        + " |"
        for result in results
    ]
    return "\n".join([header, divider, *rows])


def render_coverage(results: list[ProviderResult]) -> str:
    lines = ["| Provider | Capability | Покриття |", "|---|---|---|"]
    for result in results:
        for capability, data in sorted(result.coverage.items()):
            lines.append(
                f"| {result.name} | {capability} | "
                f"{data['covered']}/{data['checked']} |"
            )
    return "\n".join(lines) if len(lines) > 2 else "_Живих прогонів не було._"


def render_report(results: list[ProviderResult]) -> str:
    passed, blockers = gate0_passed(results)
    summary = stack_coverage(results)

    parts = [
        "# Gate 0 — результат прогону (ТЗ §3)",
        "",
        f"**Дата:** {datetime.now(UTC).date().isoformat()}",
        f"**Статус:** {'ПРОЙДЕНО' if passed else 'НЕ ПРОЙДЕНО'}",
        "",
        "## Таблиця приймання",
        "",
        render_table(results),
        "",
        "## Чим підкріплений кожен статус",
        "",
        "Статус ринку — це частка матчів вибірки, у яких ринок реально знайшовся, "
        f"а не один вдалий випадок. YES від {int(COVERAGE_YES * 100)}% покриття.",
        "",
        render_coverage(results),
        "",
        "## Підсумок по стеку",
        "",
        "| Потрібна здатність | Закрита стеком |",
        "|---|---|",
        *[f"| {c} | {s} |" for c, s in summary.items()],
        "",
    ]

    if blockers:
        parts += ["## Що заважає закрити Gate 0", ""]
        parts += [f"* {b}" for b in blockers]
        parts += [""]

    parts += ["## Деталі по провайдерах", ""]
    for result in results:
        parts += [
            f"### {result.name}",
            "",
            f"* хост: `{result.host}` — {result.reachable} ({result.reachability_detail})",
            f"* ключ: {'є' if result.has_key else 'НЕМАЄ'}",
            f"* матчів у вибірці: {result.matches_checked}"
            f"{'' if result.sample_is_sufficient else f' (менше за {MIN_MATCH_COUNT} — недостатньо для §3)'}",
            f"* per-event запитів: {result.events_probed}",
            f"* latency: {round(result.latency_ms) if result.latency_ms else '—'} ms",
            f"* приклади timestamps: {', '.join(result.sample_timestamps) or '—'}",
        ]
        if result.errors:
            parts += ["* помилки:"] + [f"  * `{e}`" for e in result.errors[:10]]
        parts += [""]

    return "\n".join(parts)


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Gate 0 — валідація провайдерів (ТЗ §3)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="лише перевірка мережі, без запитів до API")
    parser.add_argument("--matches", type=int, default=DEFAULT_MATCH_COUNT,
                        help=f"скільки матчів брати у вибірку (ТЗ §3: {MIN_MATCH_COUNT}-30)")
    parser.add_argument("--md-out", default=None, help="куди записати звіт .md")
    parser.add_argument("--json-out", default=None, help="куди записати сирий результат .json")
    args = parser.parse_args()

    if args.matches < MIN_MATCH_COUNT:
        print(
            f"УВАГА: --matches {args.matches} менше за {MIN_MATCH_COUNT}. "
            f"ТЗ §3 вимагає 20-30 матчів — гейт з такою вибіркою не зарахується.",
            file=sys.stderr,
        )

    results: list[ProviderResult] = []
    for spec in PROVIDERS:
        result = ProviderResult(name=spec.name, host=spec.host)
        result.reachable, result.reachability_detail = check_reachability(spec.host)
        api_key = os.getenv(spec.key_env, "")
        result.has_key = bool(api_key)

        if args.preflight_only:
            results.append(result)
            continue
        if result.reachable != Status.YES:
            result.mark_all(Status.BLOCKED)
            results.append(result)
            continue
        if not api_key:
            result.mark_all(Status.NO_KEY)
            results.append(result)
            continue
        if spec.probe is None:
            result.errors.append("проба не реалізована")
            results.append(result)
            continue

        try:
            spec.probe(result, api_key, args.matches)
        except Exception as exc:  # noqa: BLE001 — один провайдер не валить прогін
            result.errors.append(f"проба впала: {type(exc).__name__}: {exc}")
        results.append(result)

    report = render_report(results)
    print(report)

    if args.md_out:
        with open(args.md_out, "w", encoding="utf-8") as handle:
            handle.write(report)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "match_target": args.matches,
                    "stack_coverage": stack_coverage(results),
                    "gate0_passed": gate0_passed(results)[0],
                    "blockers": gate0_passed(results)[1],
                    "providers": [
                        {
                            "name": r.name,
                            "host": r.host,
                            "reachable": r.reachable,
                            "reachability_detail": r.reachability_detail,
                            "has_key": r.has_key,
                            "capabilities": r.capabilities,
                            "coverage": r.coverage,
                            "matches_checked": r.matches_checked,
                            "events_probed": r.events_probed,
                            "sample_timestamps": r.sample_timestamps,
                            "latency_ms": r.latency_ms,
                            "errors": r.errors,
                        }
                        for r in results
                    ],
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )

    return 0 if gate0_passed(results)[0] else 1


if __name__ == "__main__":
    raise SystemExit(main())
