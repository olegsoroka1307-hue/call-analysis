"""Оцінка релевантності замовлення.

Проста й передбачувана арифметика замість ШІ: на цьому кроці треба відсіяти
явно чуже дешево й швидко, а думати над тим, що лишилось, буде модель.
"""
from __future__ import annotations

from .config import Config
from .models import Project, Score

PRODUCT_LABELS = {
    "meetings": "Розбір зустрічей → задачі в Notion і Telegram",
    "email": "Тріаж вхідної пошти",
    "automation": "Автоматизація процесів",
}


def _group_hits(text: str, words: list[str]) -> list[str]:
    return [w for w in words if w and w.lower() in text]


def score_project(project: Project, cfg: Config) -> Score:
    text = project.searchable
    matched: list[str] = []
    contributions: dict[str, int] = {}
    total = 0

    for group, spec in cfg.keywords.items():
        words = spec.get("words") or []
        weight = int(spec.get("weight") or 1)
        hits = _group_hits(text, words)
        if not hits:
            continue
        # Базова вага за групу + по одному балу за кожне наступне слово,
        # але не більше ніж подвоєння ваги: одне влучне слово важить майже
        # стільки ж, скільки п'ять випадкових згадок.
        bonus = min(len(hits) - 1, weight)
        contributions[group] = weight + bonus
        total += weight + bonus
        matched.extend(hits)

    penalties = _group_hits(text, cfg.stop_words)
    total -= 4 * len(penalties)

    product = "automation"
    for candidate in ("meetings", "email"):
        if contributions.get(candidate):
            best = max(
                ("meetings", contributions.get("meetings", 0)),
                ("email", contributions.get("email", 0)),
                key=lambda pair: pair[1],
            )
            product = best[0]
            break

    score = Score(total=total, matched=sorted(set(matched)), product=product)

    if cfg.skip_plus_only and project.is_only_for_plus:
        score.blocked_by = "тільки для Plus-акаунтів"
    elif project.bid_count > cfg.max_bids:
        score.blocked_by = f"вже {project.bid_count} відгуків — запізно"
    elif _budget_too_low(project, cfg):
        score.blocked_by = f"бюджет {project.budget_text} нижчий за мінімум"
    elif total < cfg.min_score:
        score.blocked_by = f"релевантність {total} нижча за поріг {cfg.min_score}"
    return score


def _budget_too_low(project: Project, cfg: Config) -> bool:
    # Порожній бюджет не відсіюємо: на біржах його часто не вказують,
    # а домовляються в переписці.
    if project.budget_amount is None:
        return False
    minimum = cfg.min_budget.get(project.budget_currency)
    if minimum is None:
        return False
    return project.budget_amount < float(minimum)


def product_label(product: str) -> str:
    return PRODUCT_LABELS.get(product, PRODUCT_LABELS["automation"])
