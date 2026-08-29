# Формули — що саме реалізовано і де (ТЗ §13–§16, §36)

Довідник для рев'ю: кожна формула з посиланням на розділ ТЗ, файл і тест.

## 1. Ринкова ймовірність і no-vig (§15)

```
raw_implied_probability = 1 / odds

p_over_raw  = 1 / over_odds
p_under_raw = 1 / under_odds
p_over_no_vig = p_over_raw / (p_over_raw + p_under_raw)
```

Маржа букмекера: `overround = p_over_raw + p_under_raw - 1`.

`app/models_engine/novig.py` · `tests/test_no_vig.py`

## 2. Score matrix (§12)

```
P(home = k) = Poisson(lambda_home)
P(away = k) = Poisson(lambda_away)
score_matrix = outer(home_distribution, away_distribution)
```

Сітка 0–10 голів на команду (мінімум за §12). Хвіст за межами сітки обрізається,
тому матриця **ренормалізується** — інакше сума ймовірностей була б < 1 і всі EV
системно занижувались би. Втрачена маса при типових lambda ≈ 1e-7.

`app/models_engine/poisson.py` · `tests/test_poisson.py`

## 3. Ринки (§13)

```
lambda_total = lambda_home + lambda_away

P(Over 2.5)         = P(total_goals >= 3)
P(Home TT Over 2.5) = P(home_goals >= 3)
P(BTTS Yes)         = P(home_goals > 0 and away_goals > 0)
```

## 4. Asian quarter-lines (§14)

Розкладаються на дві половини ставки, а не наближаються однією ймовірністю:

```
Over 2.75 = 50% Over 2.5 + 50% Over 3.0
Over 3.25 = 50% Over 3.0 + 50% Over 3.5
```

Обидві половини визначаються **одним і тим самим** фактичним рахунком, тобто
ідеально корельовані. Тому розподіл результату рахується перебором розподілу
голів із застосуванням тієї ж функції розрахунку, що і в проді (§36), а не
перемноженням половин як незалежних подій.

`app/models_engine/lines.py` · `tests/test_asian_totals.py`

## 5. Fair odds, edge, EV (§16)

Уніфікований розрахунок через розподіл результату ставки:

```
A = Σ P(outcome) * win_fraction     для win_fraction > 0   (очікувана виграшна частка)
B = Σ P(outcome) * |win_fraction|   для win_fraction < 0   (очікувана програшна частка)

EV(odds)          = A * (odds - 1) - B
fair_odds         = 1 + B / A            (odds, за яких EV = 0)
model_probability = A / (A + B)          (fair probability з поправкою на push)
edge              = model_probability - market_no_vig_probability
```

**Чому так, а не `binary_EV`:** на half-лінії `A = P(win)`, `B = 1 - P(win)`, і
формули **тотожні** ТЗ §16 (`fair_odds = 1/p`, `EV = p * odds - 1`) — це
перевіряє `test_ev_matches_spec_binary_formula_on_half_line`. Але на quarter- і
цілих лініях `binary_EV` ігнорує half-win/half-loss/push і дає хибний сигнал:

| O2.75 @ 2.00, lambda 1.55/1.20 | EV | Рішення |
|---|---|---|
| binary_EV | **+3.7%** | ❌ BET |
| expected payout (§16) | **−7.4%** | ✅ PASS |

`app/models_engine/ev.py` · `tests/test_ev.py`

## 6. Розрахунок ставки (§36, §36.1)

Стани: `WIN`, `HALF_WIN`, `PUSH`, `HALF_LOSS`, `LOSS`, `VOID`, `PENDING`.

```
win_fraction: WIN +1.0 | HALF_WIN +0.5 | PUSH 0.0 | HALF_LOSS -0.5 | LOSS -1.0

pnl = win_fraction * stake * (odds - 1)   якщо win_fraction > 0
      win_fraction * stake                якщо win_fraction < 0
```

Обов'язковий тест §36.1 — **O2.75 @ 2.00**:

| Голи | Розрахунок | P/L |
|---|---|---|
| 0–2 | LOSS | −1.0u |
| 3 | HALF_WIN | +0.5u |
| 4+ | WIN | +1.0u |

`app/models_engine/settlement.py` · `tests/test_asian_totals.py::TestMandatoryAsianSettlement`

## 7. Рішення (§18, §25)

```
if EV < 0.02:                  PASS
elif EV < 0.04:                WATCH
elif disagreement > 0.07:      WATCH      # §18: розходження з ринком != value
else:                          BET
```

Не реалізовано у PoC (позначено `TODO` зі спринтом): `data_quality < 55 -> PASS`
(§23), `confidence == "C" -> WATCH` (§24), reverse-check (§26).

`app/models_engine/decision.py` · `tests/test_ev.py::TestDecisionEngine`

## 8. Історія коефіцієнтів (§7.2)

```
opening = перший валідний snapshot
current = останній валідний snapshot
closing = останній валідний snapshot СТРОГО до kickoff
```

`source_timestamp` (час провайдера) і `received_at` (час запису) зберігаються
окремо. Таблиця append-only — гарантується тригером PostgreSQL.

`app/services/movement.py` · `tests/test_storage_integration.py`
