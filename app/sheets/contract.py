"""Загрузка и проверка контракта листов config/sheets_map.yaml."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

WRITERS = ("owner", "formula", "apps_script", "n8n", "finmodel")


class ContractError(Exception):
    pass


@dataclass(frozen=True)
class SpreadsheetSpec:
    alias: str
    env: str
    title: str


@dataclass(frozen=True)
class RequiredColumn:
    name: str
    occurrence: int | None = None

    def label(self) -> str:
        return self.name if self.occurrence is None else f"{self.name} (#{self.occurrence})"


@dataclass(frozen=True)
class WriterSpec:
    writer: str
    occurrence: int | None = None


@dataclass(frozen=True)
class SheetSpec:
    alias: str
    spreadsheet: str
    title: str
    header_row: int | None = None
    data_from: int | None = None
    key: str | None = None
    required: tuple[RequiredColumn, ...] = ()
    writers: Mapping[str, WriterSpec] = field(default_factory=dict)
    optional: bool = False
    writer_future: str | None = None


@dataclass(frozen=True)
class Contract:
    version: int
    spreadsheets: Mapping[str, SpreadsheetSpec]
    sheets: tuple[SheetSpec, ...]

    def sheet(self, alias: str) -> SheetSpec:
        for s in self.sheets:
            if s.alias == alias:
                return s
        raise ContractError(f"нет листа с алиасом {alias}")

    def sheets_of(self, spreadsheet: str) -> list[SheetSpec]:
        return [s for s in self.sheets if s.spreadsheet == spreadsheet]

    def resolve_ids(self, env: Mapping[str, str]) -> dict[str, str]:
        """alias таблицы → ID из env. Ошибка называет переменную, но не значение."""
        missing = [s.env for s in self.spreadsheets.values() if not (env.get(s.env) or "").strip()]
        if missing:
            raise ContractError("не заданы переменные окружения: " + ", ".join(missing))
        return {a: env[s.env].strip() for a, s in self.spreadsheets.items()}


def _int_or_none(value: object, what: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError(f"{what}: ожидается целое ≥ 1")
    return value


def _required(items: object, where: str) -> tuple[RequiredColumn, ...]:
    if items is None:
        return ()
    if not isinstance(items, list):
        raise ContractError(f"{where}.required: ожидается список")
    out: list[RequiredColumn] = []
    for it in items:
        if isinstance(it, str) and it.strip():
            out.append(RequiredColumn(it))
        elif isinstance(it, dict) and isinstance(it.get("name"), str) and it["name"].strip():
            if set(it) - {"name", "occurrence"}:
                raise ContractError(f"{where}.required: лишние поля в {it!r}")
            out.append(RequiredColumn(it["name"], _int_or_none(it.get("occurrence"), f"{where}.occurrence")))
        else:
            raise ContractError(f"{where}.required: неверный элемент {it!r}")
    return tuple(out)


def parse_contract(data: object) -> Contract:
    if not isinstance(data, dict):
        raise ContractError("контракт: ожидается словарь верхнего уровня")
    if data.get("version") != 1:
        raise ContractError("контракт: поддерживается только version: 1")

    raw_ss = data.get("spreadsheets")
    if not isinstance(raw_ss, dict) or not raw_ss:
        raise ContractError("контракт: нет раздела spreadsheets")
    spreadsheets: dict[str, SpreadsheetSpec] = {}
    for alias, spec in raw_ss.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("env"), str) or not spec["env"]:
            raise ContractError(f"spreadsheets.{alias}: нужно поле env")
        spreadsheets[str(alias)] = SpreadsheetSpec(str(alias), spec["env"], str(spec.get("title", alias)))

    raw_sheets = data.get("sheets")
    if not isinstance(raw_sheets, list) or not raw_sheets:
        raise ContractError("контракт: нет раздела sheets")
    sheets: list[SheetSpec] = []
    seen: set[str] = set()
    allowed = {"alias", "spreadsheet", "title", "header_row", "data_from", "key", "required", "writers",
               "optional", "writer_future"}
    for raw in raw_sheets:
        if not isinstance(raw, dict):
            raise ContractError("sheets: элемент должен быть словарём")
        alias = raw.get("alias")
        if not isinstance(alias, str) or not alias:
            raise ContractError("sheets: нет alias")
        where = f"sheets.{alias}"
        if extra := set(raw) - allowed:
            raise ContractError(f"{where}: неизвестные поля {sorted(extra)}")
        if alias in seen:
            raise ContractError(f"{where}: алиас повторяется")
        seen.add(alias)
        ss = raw.get("spreadsheet")
        if ss not in spreadsheets:
            raise ContractError(f"{where}: неизвестный алиас таблицы {ss!r}")
        title = raw.get("title")
        if not isinstance(title, str) or not title:
            raise ContractError(f"{where}: нет title")
        header_row = _int_or_none(raw.get("header_row"), f"{where}.header_row")
        data_from = _int_or_none(raw.get("data_from"), f"{where}.data_from")
        required = _required(raw.get("required"), where)
        key = raw.get("key")
        if header_row is None:
            if data_from or key or required:
                raise ContractError(f"{where}: без header_row не задаются data_from/key/required")
        else:
            if data_from is None or data_from <= header_row:
                raise ContractError(f"{where}: data_from должен быть больше header_row")
            if not isinstance(key, str) or not key:
                raise ContractError(f"{where}: нужен key")
        writers = raw.get("writers") or {}
        if not isinstance(writers, dict):
            raise ContractError(f"{where}.writers: ожидается словарь")
        parsed_writers: dict[str, WriterSpec] = {}
        for col, w in writers.items():
            if isinstance(w, dict):
                if set(w) - {"writer", "occurrence"}:
                    raise ContractError(f"{where}.writers[{col}]: допустимы поля writer, occurrence")
                spec_w = WriterSpec(w.get("writer"), _int_or_none(w.get("occurrence"), f"{where}.writers[{col}]"))
            else:
                spec_w = WriterSpec(w)
            if spec_w.writer not in WRITERS:
                raise ContractError(f"{where}.writers[{col}]: допустимо {', '.join(WRITERS)}")
            parsed_writers[str(col)] = spec_w
        optional = raw.get("optional", False)
        if not isinstance(optional, bool):
            raise ContractError(f"{where}.optional: ожидается true/false")
        sheets.append(
            SheetSpec(alias=alias, spreadsheet=ss, title=title, header_row=header_row, data_from=data_from,
                      key=key, required=required, writers=parsed_writers,
                      optional=optional, writer_future=raw.get("writer_future"))
        )
    return Contract(version=1, spreadsheets=spreadsheets, sheets=tuple(sheets))


def load_contract(path: Path) -> Contract:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise ContractError(f"контракт не найден: {path.name}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ContractError(f"контракт: ошибка YAML ({type(exc).__name__})") from None
    return parse_contract(data)
