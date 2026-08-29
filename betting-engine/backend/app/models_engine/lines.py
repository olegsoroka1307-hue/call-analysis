"""Робота з лініями тоталів/гандикапів.

ТЗ §14: quarter-lines (2.25 / 2.75 / 3.25) реалізуються через split-stake,
а не через наближену probability. Тут — базові примітиви розбору лінії.
"""

from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    OVER = "OVER"
    UNDER = "UNDER"


class LineType(str, Enum):
    INTEGER = "INTEGER"   # 2.0  -> можливий PUSH
    HALF = "HALF"         # 2.5  -> тільки WIN/LOSS
    QUARTER = "QUARTER"   # 2.25 / 2.75 -> split-stake на дві половини


def _quarters(line: float) -> int:
    """Лінія у чвертях гола. Кидає ValueError, якщо лінія не кратна 0.25."""
    q = round(line * 4)
    if abs(line * 4 - q) > 1e-9:
        raise ValueError(f"line must be a multiple of 0.25, got {line!r}")
    return int(q)


def classify_line(line: float) -> LineType:
    rem = _quarters(line) % 4
    if rem == 0:
        return LineType.INTEGER
    if rem == 2:
        return LineType.HALF
    return LineType.QUARTER


def split_line(line: float) -> list[tuple[float, float]]:
    """Розкласти лінію на (під_лінія, частка_ставки).

    ТЗ §14:
        Over 2.75 = 50% Over 2.5 + 50% Over 3.0
        Over 3.25 = 50% Over 3.0 + 50% Over 3.5
    Не-quarter лінія повертається як єдина половина з вагою 1.0.
    """
    if classify_line(line) is LineType.QUARTER:
        return [(line - 0.25, 0.5), (line + 0.25, 0.5)]
    return [(line, 1.0)]
