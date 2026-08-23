"""Куди складати результат: CSV на диску або Google Sheets."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Protocol

from .models import SHEET_HEADER, TriageResult


class Sink(Protocol):
    def write(self, results: list[TriageResult]) -> int: ...
    def describe(self) -> str: ...


class CsvSink:
    """Працює без жодних доступів до Google — саме тому це стандартний вихід."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, results: list[TriageResult]) -> int:
        if not results:
            return 0
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(SHEET_HEADER)
            for result in results:
                writer.writerow(result.as_row())
        return len(results)

    def describe(self) -> str:
        return f"CSV: {self.path}"


class SheetsSink:
    """Дописує рядки в аркуш. Наявні рядки ніколи не перезаписує."""

    def __init__(self, spreadsheet_id: str, sheet_name: str, service) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self.service = service

    def ensure_header(self) -> None:
        existing = (
            self.service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=f"{self.sheet_name}!A1:A1")
            .execute()
        )
        if not existing.get("values"):
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{self.sheet_name}!A1",
                valueInputOption="RAW",
                body={"values": [SHEET_HEADER]},
            ).execute()

    def write(self, results: list[TriageResult]) -> int:
        if not results:
            return 0
        self.ensure_header()
        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"{self.sheet_name}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [r.as_row() for r in results]},
        ).execute()
        return len(results)

    def describe(self) -> str:
        return (
            f"Google Sheets: https://docs.google.com/spreadsheets/d/"
            f"{self.spreadsheet_id} (аркуш «{self.sheet_name}»)"
        )
