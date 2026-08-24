from __future__ import annotations

from datetime import date

from meetings.report import build_rows, execution_rate, render

TODAY = date(2026, 8, 24)


def task(**kwargs) -> dict:
    base = {
        "responsible": "Саша", "task": "КП", "status": "Не начата",
        "deadline": "", "priority": "Средний", "project": "", "source": "Планірка",
    }
    base.update(kwargs)
    return base


def test_overdue_is_counted_from_the_deadline():
    rows = build_rows([task(deadline="2026-08-20")], today=TODAY)
    assert rows[0].overdue_days == 4


def test_future_deadline_is_not_overdue():
    rows = build_rows([task(deadline="2026-09-01")], today=TODAY)
    assert rows[0].overdue_days == 0


def test_done_task_is_never_overdue():
    rows = build_rows([task(deadline="2026-01-01", status="Готово")], today=TODAY)
    assert rows[0].overdue_days == 0


def test_cancelled_task_is_never_overdue():
    rows = build_rows([task(deadline="2026-01-01", status="Отменена")], today=TODAY)
    assert rows[0].overdue_days == 0


def test_broken_date_does_not_crash_the_report():
    rows = build_rows([task(deadline="колись")], today=TODAY)
    assert rows[0].overdue_days == 0


def test_most_overdue_comes_first():
    rows = build_rows(
        [
            task(task="свіже", deadline="2026-08-23"),
            task(task="давнє", deadline="2026-07-01"),
            task(task="без терміну"),
        ],
        today=TODAY,
    )
    assert [r.task for r in rows] == ["давнє", "свіже", "без терміну"]


def test_execution_rate_ignores_cancelled():
    # Інакше показник накручується скасуванням незручних задач.
    rows = build_rows(
        [
            task(task="1", status="Готово"),
            task(task="2", status="Не начата"),
            task(task="3", status="Отменена"),
        ],
        today=TODAY,
    )
    assert execution_rate(rows) == 50


def test_execution_rate_of_nothing_is_zero():
    assert execution_rate([]) == 0


def test_each_task_appears_in_exactly_one_block():
    rows = build_rows(
        [
            task(task="зроблена", status="Готово"),
            task(task="прострочена", status="В работе", deadline="2026-08-01"),
            task(task="в роботі", status="В работе", deadline="2026-09-10"),
            task(task="відкладена", status="Отложено"),
            task(task="скасована", status="Отменена"),
            task(task="нова"),
        ],
        today=TODAY,
    )
    text = render(rows, meeting="Планірка", today=TODAY)
    for name in ("зроблена", "прострочена", "в роботі", "відкладена", "скасована", "нова"):
        assert text.count(name) == 1
    assert "прострочення 23 дн." in text
    assert "Виконано: 20%" in text


def test_empty_report_says_so():
    text = render([], today=TODAY)
    assert "Задач немає" in text
