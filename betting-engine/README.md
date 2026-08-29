# Betting Decision Engine — Gate 0 + PoC (тестове завдання на 1 день)

Реалізовано два розділи ТЗ, які мають бути закриті **до** старту основної розробки:

| Розділ ТЗ | Що це | Статус |
|---|---|---|
| **§3 — Gate 0** | валідація data providers на 20–30 матчах | 🔴 **не пройдений — заблоковано мережею** ([звіт](gate0/GATE0_REPORT.md)) |
| **§52 — PoC на 1 день** | наскрізний ланцюг odds → EV → рішення | 🟢 **готовий, працює, 109 тестів зелені** |

---

## 1. Головне, що треба знати одразу

**Gate 0 не пройдений, і це не питання коду.** Усі хости data-провайдерів
(`api.the-odds-api.com`, `v3.football.api-sports.io`, `api.sportmonks.com` та
інші) заблоковані egress-політикою середовища — проксі відповідає `403` ще на
етапі `CONNECT`, запит не доходить до провайдера. Деталі, наслідки та що саме
потрібно від замовника — у [gate0/GATE0_REPORT.md](gate0/GATE0_REPORT.md).

За ТЗ §48 це означає: **до Sprint 1 переходити не можна**, доки Gate 0 не закритий.

**Через це в PoC немає реальних коефіцієнтів.** ТЗ §1 забороняє вигадувати дані,
тому замість «намалювати правдоподібні числа» зроблено так:

* увесь ланцюг працює на **синтетичному датасеті**, який лежить у форматі
  реальної відповіді The Odds API v4;
* датасет проходить **той самий парсер**, що й бойовий HTTP — підмінена лише
  мережа, не логіка;
* мітка `SYNTHETIC_REPLAY` протягнута від файлу даних через БД до кожної
  відповіді API — сплутати ці дані з реальним ринком неможливо.

Перемикання на реальні дані — це **один рядок конфігу**, без змін у коді:

```bash
ODDS_PROVIDER=the_odds_api
ODDS_API_KEY=<ключ>
```

---

## 2. Запуск без Docker (перевірений шлях)

```bash
# 1. PostgreSQL 16 має бути запущений
createdb betting_poc

# 2. Залежності
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

# 3. Міграції
cd backend
export DATABASE_URL="postgresql+psycopg://postgres@127.0.0.1:5432/betting_poc"
alembic upgrade head

# 4. Датасет (уже в репозиторії; регенерація — щоб матчі були в майбутньому)
python -m app.tools.make_replay_dataset --days-ahead 3

# 5. Повний прогін ТЗ §52, пункти 1–8
python -m app.tools.run_poc

# 6. API
uvicorn app.main:app --reload
#    -> http://127.0.0.1:8000/poc/report
#    -> http://127.0.0.1:8000/docs

# 7. Тести
pytest
```

### Docker Compose

`docker-compose.yml` написаний за ТЗ §47, але **не перевірений прогоном** —
у середовищі розробки не було docker-демона. Перевірений шлях — локальний вище.
Це свідомо вказано, щоб критерій приймання §51 («запускається через Docker
Compose») не вважався закритим без реальної перевірки.

---

## 3. ТЗ §52 — пункт за пунктом

| # | Вимога | Де реалізовано | Перевірено |
|---|---|---|---|
| 1 | Підключити 1 odds API | `app/providers/the_odds_api.py` (HTTP) + `replay.py` (офлайн) | ✅ |
| 2 | 5 реальних майбутніх матчів | `app/tools/make_replay_dataset.py` — 5 матчів; ⚠️ **синтетичні**, бо API заблоковані | ⚠️ |
| 3 | Fixtures у PostgreSQL | `app/db/models.py`, `app/services/ingest.py` | ✅ |
| 4 | 3–5 odds snapshots на тотальний ринок | `insert_odds_snapshots` — 5 опитувань, append-only | ✅ |
| 5 | Opening / current movement | `app/services/movement.py` | ✅ |
| 6 | No-vig probability | `app/models_engine/novig.py` | ✅ |
| 7 | Poisson: O2.5 і team O2.5 | `app/models_engine/poisson.py` | ✅ |
| 8 | Fair odds та EV | `app/models_engine/ev.py` | ✅ |
| 9 | Результат через FastAPI | `app/main.py` → `GET /poc/report` | ✅ |
| 10 | ≥5 unit-тестів + O2.75 settlement | `tests/` — **109 тестів** | ✅ |

Єдиний пункт із застереженням — №2: матчі синтетичні. Це прямий наслідок
незакритого Gate 0, а не спрощення.

---

## 4. Технічні рішення, які варто побачити при рев'ю

### 4.1 Asian quarter-lines рахуються через split-stake, а не приблизно (§14, §16)

Це не деталь — це місце, де «майже правильно» дає хибний сигнал на ставку.
Для O2.75 при кефі 2.00 і lambda 1.55/1.20:

| Спосіб | EV | Рішення |
|---|---|---|
| binary_EV на «ймовірності виграшу» | **+3.7%** | ❌ BET |
| expected payout по §14/§16 | **−7.4%** | ✅ PASS |

Різниця в 11 п.п. і протилежне рішення. Тест
`test_binary_formula_would_overstate_quarter_line_value` фіксує саме це.

Реалізація уніфікована: `A` — очікувана виграшна частка ставки, `B` — програшна.

```
EV        = A * (odds - 1) - B
fair_odds = 1 + B / A
model_p   = A / (A + B)
```

На half-лінії це **математично тотожне** формулам ТЗ §16 — є тест, який
звіряє обидва шляхи до 6 знаків.

### 4.2 Append-only гарантується базою, а не домовленістю (§7.2, §2)

На `odds_snapshots` стоїть тригер PostgreSQL: будь-який `UPDATE`/`DELETE` падає з
помилкою — з застосунку, з psql, з міграції.

```
ERROR: odds_snapshots is append-only (spec 7.2): UPDATE is not allowed
```

Межа дії чесно задокументована в міграції: row-level тригер не ловить `TRUNCATE`
і `DROP` — інакше зламався б `alembic downgrade`. У проді це закривається
правами ролі.

### 4.3 Сильне розходження з ринком не стає автоматичним BET (§18, §25)

На синтетичних даних тестова lambda дала EV **+25.8%** на одному ринку. Це не
value, а артефакт того, що одна lambda застосована до всіх матчів. Ланцюг §25
коректно понижує це до `WATCH` через `market_disagreement > 0.07`, а не пускає
в `BET`. Нереалізовані гілки (`data_quality` §23, `confidence` §24) позначені
явними `TODO` з номером спринту — а не тихо пропущені.

### 4.4 Відсутні дані не підмінюються здогадками (§1)

* немає протилежної ціни → `market_probability = null` + код
  `NO_VIG_UNAVAILABLE_MISSING_OPPOSITE_SIDE`, а не «приблизно 50%»;
* немає ключа API → `ProviderError` одразу, а не тихий фолбек;
* `data_quality_score` ще не рахується → в БД `NULL`, а не вигадане число;
* невідомий ринок або команда в payload → пропускається, а не вгадується.

### 4.5 Prediction відтворювана (§51)

`model_runs.inputs_json` зберігає lambda, `max_goals`, `model_version` і `id`
використаних снапшотів. Тест `test_prediction_can_be_recomputed_from_inputs_json`
перераховує кожну prediction з нуля лише за цим JSON і звіряє числа.

---

## 5. Що НЕ реалізовано (і це навмисно)

PoC — це §52, а не MVP. Свідомо поза межами, з номерами спринтів із ТЗ §48:

| Не реалізовано | Розділ ТЗ | Спринт |
|---|---|---|
| Реальні lambda зі статистики, rolling metrics | §9, §10, §11 | Sprint 2 |
| Dixon-Coles, market blend | §11, §17 | Sprint 10 / 1.1 |
| Data Quality Score, Confidence A/B/C | §23, §24 | Sprint 6 |
| Reverse engine | §26 | Sprint 7 |
| Scheduler / polling | §27 | Sprint 5 |
| Telegram alerts | §29 | Sprint 7 |
| Frontend | §30–§34 | Sprint 4 |
| Bet journal, CLV, calibration, backtesting | §35–§41 | Sprint 8–10 |

Таблиці `team_match_stats`, `injuries`, `lineups`, `alerts`, `bets` з §7.1 не
створені — вони належать пізнішим спринтам і без даних були б порожніми.

---

## 6. Відхилення від ТЗ

1. **Python 3.11 замість 3.12+ (§4)** — у середовищі доступний 3.11. Код на 3.12
   сумісний (Dockerfile використовує `python:3.12-slim`), але **перевірений
   прогоном саме на 3.11**.
2. **Матчі синтетичні (§52 п.2)** — прямий наслідок незакритого Gate 0.
3. **Docker Compose не перевірений** — немає docker-демона в середовищі.
4. **TimescaleDB не використана (§4)** — для 5 матчів звичайного PostgreSQL
   достатньо; питання актуальне на обсягах Sprint 1+.

---

## 7. Структура

```
betting-engine/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI (§45, §52 п.9)
│   │   ├── config.py            # §47
│   │   ├── db/models.py         # §7.1
│   │   ├── providers/           # §6 — abstraction + The Odds API + replay
│   │   ├── models_engine/       # §11-§16, §18, §25, §36 — уся математика
│   │   ├── services/            # ingest, movement, pricing
│   │   └── tools/               # run_poc, make_replay_dataset
│   ├── migrations/              # alembic, вкл. append-only тригер
│   ├── tests/                   # 109 тестів (§46)
│   └── data/                    # синтетичний датасет
├── gate0/
│   ├── validate_providers.py    # harness §3
│   ├── GATE0_REPORT.md          # звіт §3
│   └── gate0_result.json        # сирий результат прогону
└── docker-compose.yml           # §47 (не перевірений)
```

---

## 8. Наступний крок

Розблокувати Gate 0: доступ до мережі + два API-ключі (odds-провайдер і
stats-провайдер). Після цього — один прогін харнесу, і рішення про Sprint 1
ухвалюється на реальних цифрах, а не на очікуваннях.
