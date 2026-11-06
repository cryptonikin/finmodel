"""Общие подделки и фикстуры. Сети нет: все данные таблиц выдуманы."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.context import AppContext
from app.db import Database
from app.settings import REPO_ROOT, Settings
from app.sheets.client import SheetInfo, SheetsAccessError
from app.sheets.headers import column_letter

FAKE_IDS = {"SHEET_INVEST_ID": "fake-invest", "SHEET_BUDGET_ID": "fake-budget"}
_RANGE = re.compile(r"^'((?:[^']|'')+)'!(?:([A-Z]+)?(\d+):([A-Z]+)?(\d+))$")


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


class FakeSheetsReader:
    """Таблицы в памяти: {spreadsheet_id: {title: [[row]...]}}. Поддерживает A1-диапазоны 'T'!4:4 и 'T'!C5:C100."""

    def __init__(self, books: dict[str, dict[str, list[list[str]]]], grids: dict | None = None,
                 fail: str | None = None) -> None:
        self.books = books
        self.grids = grids or {}
        self.fail = fail
        self.calls: list[tuple[str, object]] = []

    def list_sheets(self, spreadsheet_id: str) -> list[SheetInfo]:
        self.calls.append(("list_sheets", spreadsheet_id))
        if self.fail == "list":
            raise SheetsAccessError("нет доступа (HTTP 403)")
        book = self.books[spreadsheet_id]
        out = []
        for i, (title, rows) in enumerate(book.items()):
            rc, cc = self.grids.get(title, (max(len(rows), 1) + 10, max((len(r) for r in rows), default=1)))
            out.append(SheetInfo(title, i, rc, cc))
        return out

    def batch_get(self, spreadsheet_id: str, ranges: Sequence[str]) -> list[list[list[str]]]:
        self.calls.append(("batch_get", list(ranges)))
        if self.fail == "batch":
            raise SheetsAccessError("HTTP 500 после 3 попыток")
        out = []
        for r in ranges:
            m = _RANGE.match(r)
            assert m, r
            title = m.group(1).replace("''", "'")
            c1, r1, c2, r2 = m.group(2), int(m.group(3)), m.group(4), int(m.group(5))
            rows = self.books[spreadsheet_id][title][r1 - 1:r2]
            if c1:
                a, b = _col_index(c1), _col_index(c2)
                rows = [row[a:b + 1] for row in rows]
            # как API: без хвостовых пустых ячеек и строк
            rows = [list(row) for row in rows]
            for row in rows:
                while row and row[-1] == "":
                    row.pop()
            while rows and not rows[-1]:
                rows.pop()
            out.append(rows)
        return out


def grid(header_row: int, headers: list[str], data: list[list[str]]) -> list[list[str]]:
    rows: list[list[str]] = [[] for _ in range(header_row - 1)]
    rows.append(headers)
    rows.extend(data)
    return rows


def make_books(contract, *, drop_sheets=(), drop_headers=None, extra_headers=None, data_rows=3,
               settings_rows=None):
    """Выдуманная книга, удовлетворяющая контракту. drop_headers/extra_headers: {title: [...]}."""
    drop_headers = drop_headers or {}
    extra_headers = extra_headers or {}
    books: dict[str, dict[str, list[list[str]]]] = {"fake-invest": {}, "fake-budget": {}}
    target = {"invest": "fake-invest", "budget": "fake-budget"}
    for spec in contract.sheets:
        if spec.title in drop_sheets:
            continue
        book = books[target[spec.spreadsheet]]
        if spec.header_row is None:
            book[spec.title] = [["блок"], ["x"]]
            continue
        headers: list[str] = []
        for req in spec.required:
            headers.append(req.name)
        for col in spec.writers:
            if col not in headers:
                headers.append(col)
        if spec.title == "Акции":  # дубли во 2-м блоке, как в реальной таблице
            headers += ["Баллы", "P/E", "EV/EBITDA", "Долг/EBITDA"]
        headers = [h for h in headers if h not in drop_headers.get(spec.title, [])]
        headers += extra_headers.get(spec.title, [])
        if spec.title == "Docker: настройки" and settings_rows is not None:
            book[spec.title] = [headers, *settings_rows]
            continue
        key_idx = headers.index(spec.key) if spec.key in headers else 0
        data = []
        for i in range(data_rows):
            row = [""] * len(headers)
            row[key_idx] = f"K{i + 1}"
            data.append(row)
        book[spec.title] = grid(spec.header_row, headers, data)
    return books


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "google_sa").write_text('{"type": "service_account"}', encoding="utf-8")
    env = {**FAKE_IDS, "TZ": "Europe/Moscow"}
    return Settings(data_dir=tmp_path / "data", secrets_dir=secrets, config_dir=REPO_ROOT / "config",
                    tz="Europe/Moscow", env=env)


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 11, 6, 7, 31, tzinfo=UTC)  # 10:31 МСК


@pytest.fixture
def ctx(settings: Settings, fixed_now: datetime):
    db = Database(settings.db_path)
    c = AppContext(settings=settings, db=db, now=lambda: fixed_now)
    yield c
    db.close()


__all__ = ["FakeSheetsReader", "column_letter", "grid", "make_books"]
