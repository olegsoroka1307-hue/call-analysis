"""Звіт «що обіцяли — що зробили».

Рахує не «скільки задач у роботі», а скільки з обіцяного справді закрито.
Це єдина цифра, заради якої власник узагалі відкриє цей звіт.
"""
from __future__ import annotations

from datetime import date

from .models import CANCELLED, DONE, IN_PROGRESS, POSTPONED, ReportRow

_DONE_SET = (DONE,)


def _overdue_days(deadline: str, status: str, today: date) -> int:
    if status in (DONE, CANCELLED) or not deadline:
        return 0
    try:
        due = date.fromisoformat(deadline[:10])
    except ValueError:
        return 0
    return max((today - due).days, 0)


def build_rows(tasks: list[dict], *, today: date | None = None) -> list[ReportRow]:
    today = today or date.today()
    rows = [
        ReportRow(
            responsible=task.get("responsible", ""),
            task=task.get("task", ""),
            status=task.get("status", ""),
            deadline=task.get("deadline", ""),
            overdue_days=_overdue_days(
                task.get("deadline", ""), task.get("status", ""), today
            ),
        )
        for task in tasks
    ]
    # Спершу прострочене, і серед нього — найдавніше.
    rows.sort(key=lambda r: (-r.overdue_days, r.responsible, r.task))
    return rows


def execution_rate(rows: list[ReportRow]) -> int:
    """Відсоток виконаного. Скасовані задачі не рахуються ні в чисельник,
    ні в знаменник — інакше показник накручується скасуванням."""
    counted = [r for r in rows if r.status != CANCELLED]
    if not counted:
        return 0
    done = sum(1 for r in counted if r.status in _DONE_SET)
    return round(done * 100 / len(counted))


def render(rows: list[ReportRow], *, meeting: str = "", today: date | None = None) -> str:
    today = today or date.today()
    # Кожен рядок потрапляє рівно в один блок. Прострочення перебиває статус:
    # «в роботі» з дедлайном тиждень тому — це не робота, це прострочення.
    done: list[ReportRow] = []
    overdue: list[ReportRow] = []
    in_progress: list[ReportRow] = []
    postponed: list[ReportRow] = []
    cancelled: list[ReportRow] = []
    waiting: list[ReportRow] = []
    for row in rows:
        if row.status == DONE:
            done.append(row)
        elif row.status == CANCELLED:
            cancelled.append(row)
        elif row.overdue_days > 0:
            overdue.append(row)
        elif row.status == IN_PROGRESS:
            in_progress.append(row)
        elif row.status == POSTPONED:
            postponed.append(row)
        else:
            waiting.append(row)

    out = [
        "═" * 58,
        "ЗВІТ ПО ДОМОВЛЕНОСТЯХ",
        f"Зустріч: {meeting}" if meeting else "Усі зустрічі",
        f"Сформовано: {today.isoformat()}",
        "═" * 58,
    ]
    if not rows:
        out.append("")
        out.append("Задач немає — або база порожня, або фільтр не знайшов збігів.")
        out.append("═" * 58)
        return "\n".join(out)

    def block(title: str, items: list[ReportRow], line) -> None:
        if not items:
            return
        out.append("")
        out.append(f"{title} ({len(items)}):")
        out.extend(f"  {line(r)}" for r in items)

    block("✅ ЗРОБЛЕНО", done, lambda r: f"{r.responsible} — {r.task}")
    block(
        "❌ ПРОСТРОЧЕНО", overdue,
        lambda r: f"{r.responsible} — {r.task} (прострочення {r.overdue_days} дн.)",
    )
    block(
        "⏳ У РОБОТІ", in_progress,
        lambda r: f"{r.responsible} — {r.task}"
                  + (f" (до {r.deadline})" if r.deadline else ""),
    )
    block(
        "🕐 ВІДКЛАДЕНО", postponed,
        lambda r: f"{r.responsible} — {r.task}"
                  + (f" (до {r.deadline})" if r.deadline else ""),
    )
    block(
        "◻️ НЕ РОЗПОЧАТО", waiting,
        lambda r: f"{r.responsible} — {r.task}"
                  + (f" (до {r.deadline})" if r.deadline else ""),
    )
    block("🚫 СКАСОВАНО", cancelled, lambda r: f"{r.responsible} — {r.task}")

    out.append("")
    out.append("═" * 58)
    out.append(f"Виконано: {execution_rate(rows)}%")
    if overdue:
        out.append(f"Прострочено задач: {len(overdue)}")
    out.append("═" * 58)
    return "\n".join(out)
