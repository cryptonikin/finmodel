"""Задача check (Ф0): проверка таблиц ТОЛЬКО на чтение (TASK_F0 §7, §10.1, §11.1, §12, §13.1).

Запросы к API на запуск: по таблице — список листов (1) + при наличии листов с шапкой заголовки (1)
+ ключевые колонки (1). Для двух таблиц контракта: 2 + 1 + 1 = 4. В таблицы ничего не пишется.

Коды выхода: 0 — всё найдено; 1 — пропуски/неоднозначности; 2 — нет доступа/секретов/конфигурации.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, TextIO
from zoneinfo import ZoneInfo

from app.context import AppContext, ConfigError
from app.db import from_db_time, to_db_time
from app.jobs import JobResult
from app.jobs.cleanup import META_USER_SETTINGS, size_warnings
from app.settings import SettingsError
from app.sheets.client import SheetsAccessError, SheetsReader, a1_sheet
from app.sheets.contract import Contract, ContractError, SheetSpec
from app.sheets.headers import (
    AmbiguousColumn,
    ColumnNotFound,
    candidates,
    column_letter,
    find_column,
    find_duplicates,
    last_nonempty_index,
)
from app.user_settings import (
    MAX_ROWS,
    SETTINGS_ALIAS,
    SettingRule,
    SettingsRulesError,
    UserSettings,
    defaults,
    parse_rows,
)

log = logging.getLogger(__name__)

EXIT_OK, EXIT_MISSING, EXIT_ACCESS = 0, 1, 2
EMPTY_TAIL_ROWS = 300  # §12: пустой хвост больше — предупреждение
EXTRA_COLUMNS = 5  # §12: колонок больше, чем последняя непустая в шапке + 5 — предупреждение
META_DOCKER_REMINDER = "check.docker_cleanup_reminded_at"


@dataclass
class ColumnResult:
    name: str
    status: str  # found | missing | ambiguous
    letter: str | None = None
    match: str = ""  # exact | prefix
    detail: str = ""


@dataclass
class SheetResult:
    alias: str
    title: str
    spreadsheet: str
    status: str  # ok | missing | missing_optional | no_header
    optional: bool = False
    row_count: int = 0
    column_count: int = 0
    header_last_col: int | None = None  # 1-based
    key_letter: str | None = None
    last_data_row: int | None = None
    columns: list[ColumnResult] = field(default_factory=list)
    duplicates: dict[str, list[str]] = field(default_factory=dict)
    writers: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def problems(self) -> int:
        bad = sum(1 for c in self.columns if c.status != "found")
        return bad + (1 if self.status == "missing" else 0)


@dataclass
class CheckResult:
    started_at: str
    exit_code: int = EXIT_OK
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sheets: list[SheetResult] = field(default_factory=list)
    requests: int = 0
    rows_read: int = 0
    settings_source: str = "defaults"
    settings_values: dict[str, Any] = field(default_factory=dict)
    settings_warnings: list[str] = field(default_factory=list)


class _CountingReader:
    """Считает логические запросы к API (ретраи внутри клиента не в счёт)."""

    def __init__(self, inner: SheetsReader) -> None:
        self.inner = inner
        self.calls = 0

    def list_sheets(self, spreadsheet_id: str):
        self.calls += 1
        return self.inner.list_sheets(spreadsheet_id)

    def batch_get(self, spreadsheet_id: str, ranges):
        if not ranges:
            return []
        self.calls += 1
        return self.inner.batch_get(spreadsheet_id, ranges)


def _check_columns(spec: SheetSpec, headers: list[str], res: SheetResult) -> None:
    for req in spec.required:
        try:
            idx = find_column(headers, req.name, req.occurrence)
        except AmbiguousColumn as exc:
            res.columns.append(ColumnResult(req.label(), "ambiguous", detail=str(exc)))
        except ColumnNotFound as exc:
            res.columns.append(ColumnResult(req.label(), "missing", detail=str(exc)))
        else:
            _, how = candidates(headers, req.name)
            res.columns.append(ColumnResult(req.label(), "found", column_letter(idx), how))

    res.duplicates = {name: [column_letter(i) for i in idx] for name, idx in find_duplicates(headers).items()}

    counts: Counter[str] = Counter()
    for col, writer in spec.writers.items():
        try:
            find_column(headers, col)
        except (ColumnNotFound, AmbiguousColumn) as exc:
            res.warnings.append(f"writers: {exc}")
        else:
            counts[writer] += 1
    res.writers = dict(sorted(counts.items()))

    last = last_nonempty_index(headers)
    res.header_last_col = last + 1 if last >= 0 else 0


def _size_check(res: SheetResult, spec: SheetSpec) -> None:
    if res.last_data_row is not None and res.row_count - res.last_data_row > EMPTY_TAIL_ROWS:
        res.warnings.append(
            f"размер: пустой хвост {res.row_count - res.last_data_row} строк (> {EMPTY_TAIL_ROWS}): "
            f"данные до {res.last_data_row}, всего {res.row_count}"
        )
    if res.header_last_col is not None and res.column_count > res.header_last_col + EXTRA_COLUMNS:
        res.warnings.append(
            f"размер: колонок {res.column_count}, последняя заполненная в шапке — "
            f"{column_letter(res.header_last_col - 1) if res.header_last_col else '-'} ({res.header_last_col}) "
            f"> +{EXTRA_COLUMNS}"
        )


def run_check(
    contract: Contract,
    ids: dict[str, str],
    reader: SheetsReader,
    *,
    rules: dict[str, SettingRule] | None = None,
    started_at: str = "",
) -> CheckResult:
    result = CheckResult(started_at=started_at)
    counting = _CountingReader(reader)
    settings: UserSettings | None = None

    for ss_alias in contract.spreadsheets:
        specs = contract.sheets_of(ss_alias)
        if not specs:
            continue
        sid = ids[ss_alias]
        try:
            infos = {s.title: s for s in counting.list_sheets(sid)}
        except SheetsAccessError as exc:
            result.errors.append(f"{ss_alias}: {exc}")
            result.exit_code = EXIT_ACCESS
            continue

        present: list[tuple[SheetSpec, SheetResult]] = []
        for spec in specs:
            info = infos.get(spec.title)
            if info is None:
                status = "missing_optional" if spec.optional else "missing"
                result.sheets.append(SheetResult(spec.alias, spec.title, ss_alias, status, spec.optional))
                continue
            res = SheetResult(spec.alias, spec.title, ss_alias, "ok" if spec.header_row else "no_header",
                              spec.optional, info.row_count, info.column_count)
            result.sheets.append(res)
            if spec.header_row:
                present.append((spec, res))

        # запрос 1: строки заголовков (лист настроек — целиком, он маленький)
        header_ranges = []
        for spec, res in present:
            q = a1_sheet(spec.title)
            if spec.alias == SETTINGS_ALIAS:
                header_ranges.append(f"{q}!A{spec.header_row}:Z{max(spec.header_row, min(res.row_count, MAX_ROWS))}")
            else:
                header_ranges.append(f"{q}!{spec.header_row}:{spec.header_row}")
        try:
            header_values = counting.batch_get(sid, header_ranges)
        except SheetsAccessError as exc:
            result.errors.append(f"{ss_alias}: {exc}")
            result.exit_code = EXIT_ACCESS
            continue

        key_ranges: list[tuple[SheetSpec, SheetResult, int, str]] = []
        for (spec, res), rows in zip(present, header_values, strict=True):
            headers = rows[0] if rows else []
            result.rows_read += len(rows)
            _check_columns(spec, headers, res)
            if spec.alias == SETTINGS_ALIAS and rules is not None:
                settings = parse_rows(rows, rules)
            try:
                key_idx = find_column(headers, spec.key or "")
            except (ColumnNotFound, AmbiguousColumn) as exc:
                res.warnings.append(f"ключевая колонка: {exc} — конец данных не определён")
                continue
            res.key_letter = column_letter(key_idx)
            if spec.data_from and res.row_count >= spec.data_from:
                key_ranges.append((spec, res, key_idx,
                                   f"{a1_sheet(spec.title)}!{res.key_letter}{spec.data_from}:"
                                   f"{res.key_letter}{res.row_count}"))
            else:
                res.last_data_row = spec.header_row

        # запрос 2: ключевые колонки → последняя непустая строка данных
        try:
            key_values = counting.batch_get(sid, [r for *_, r in key_ranges])
        except SheetsAccessError as exc:
            result.errors.append(f"{ss_alias}: {exc}")
            result.exit_code = EXIT_ACCESS
            continue
        for (spec, res, _, _), rows in zip(key_ranges, key_values, strict=True):
            result.rows_read += len(rows)
            col = [r[0] if r else "" for r in rows]
            last = last_nonempty_index(col)
            res.last_data_row = (spec.data_from + last) if last >= 0 else spec.header_row

        for spec, res in present:
            _size_check(res, spec)

    if rules is not None:
        if settings is None:
            settings_sheet = next((s for s in result.sheets if s.alias == SETTINGS_ALIAS), None)
            reason = "лист «Docker: настройки» не найден" if settings_sheet is None or \
                settings_sheet.status.startswith("missing") else "лист настроек не прочитан"
            settings = defaults(rules, f"{reason} — значения по умолчанию")
        result.settings_source = settings.source
        result.settings_values = settings.values
        result.settings_warnings = settings.warnings

    result.requests = counting.calls
    if result.exit_code != EXIT_ACCESS and any(s.problems for s in result.sheets):
        result.exit_code = EXIT_MISSING
    return result


# ---------- вывод ----------

_STATUS_RU = {"ok": "ok", "missing": "НЕТ ЛИСТА", "missing_optional": "нет (необязательный)",
              "no_header": "есть (без шапки)"}


def _summary(r: CheckResult) -> str:
    missing = sum(1 for s in r.sheets for c in s.columns if c.status == "missing")
    ambiguous = sum(1 for s in r.sheets for c in s.columns if c.status == "ambiguous")
    no_sheet = sum(1 for s in r.sheets if s.status == "missing")
    warns = len(r.warnings) + len(r.settings_warnings) + sum(len(s.warnings) for s in r.sheets)
    return (f"код {r.exit_code}; листов {len(r.sheets)}, нет листов {no_sheet}, "
            f"не найдено колонок {missing}, неоднозначно {ambiguous}, предупреждений {warns}, "
            f"запросов к API {r.requests}")


def render_console(r: CheckResult) -> str:
    lines = [f"finmodel check — {r.started_at}"]
    for e in r.errors:
        lines.append(f"ОШИБКА: {e}")
    if r.sheets:
        lines.append(f"{'лист':<22} {'статус':<22} {'колонки':>9} {'данные до':>10} {'строк×кол':>11}  писатели")
        for s in r.sheets:
            found = sum(1 for c in s.columns if c.status == "found")
            cols = f"{found}/{len(s.columns)}" if s.columns else "-"
            last = str(s.last_data_row) if s.last_data_row is not None else "-"
            grid = f"{s.row_count}×{s.column_count}" if s.row_count else "-"
            writers = ", ".join(f"{k} {v}" for k, v in s.writers.items()) or "-"
            lines.append(f"{s.title[:22]:<22} {_STATUS_RU[s.status]:<22} {cols:>9} {last:>10} {grid:>11}  {writers}")
            for c in s.columns:
                if c.status != "found":
                    lines.append(f"    ✖ {c.detail}")
            if s.duplicates:
                dups = "; ".join(f"«{k}» {','.join(v)}" for k, v in s.duplicates.items())
                lines.append(f"    дубли заголовков: {dups}")
            for w in s.warnings:
                lines.append(f"    ⚠ {w}")
    for w in r.settings_warnings:
        lines.append(f"⚠ настройки: {w}")
    for w in r.warnings:
        lines.append(f"⚠ {w}")
    lines.append("Итог: " + _summary(r))
    return "\n".join(lines)


def render_markdown(r: CheckResult) -> str:
    lines = [f"# Проверка таблиц (check) — {r.started_at}", "", f"**Итог:** {_summary(r)}", "",
             "Только чтение: в таблицы ничего не записано.", ""]
    if r.errors:
        lines += ["## Ошибки", *[f"- {e}" for e in r.errors], ""]
    if r.sheets:
        lines += ["## Листы", "", "| Таблица | Лист | Статус | Колонки | Данные до строки | Сетка | Писатели |",
                  "|---|---|---|---:|---:|---|---|"]
        for s in r.sheets:
            found = sum(1 for c in s.columns if c.status == "found")
            cols = f"{found}/{len(s.columns)}" if s.columns else "-"
            last = str(s.last_data_row) if s.last_data_row is not None else "-"
            grid = f"{s.row_count}×{s.column_count}" if s.row_count else "-"
            writers = ", ".join(f"{k} {v}" for k, v in s.writers.items()) or "-"
            lines.append(
                f"| {s.spreadsheet} | {s.title} | {_STATUS_RU[s.status]} | {cols} | {last} | {grid} | {writers} |"
            )
        lines.append("")
        for s in r.sheets:
            if not (s.columns or s.duplicates or s.warnings):
                continue
            lines += [f"### {s.title}", ""]
            if s.key_letter:
                lines.append(f"Ключевая колонка: {s.key_letter}")
            for c in s.columns:
                mark = {"found": "✅", "missing": "❌", "ambiguous": "⚠️"}[c.status]
                where = f" → {c.letter}" + (" (по началу)" if c.match == "prefix" else "") if c.letter else ""
                lines.append(f"- {mark} {c.name}{where}" + (f" — {c.detail}" if c.status != "found" else ""))
            if s.duplicates:
                dups = "; ".join(f"«{k}» ({', '.join(v)})" for k, v in s.duplicates.items())
                lines.append(f"- Дубли заголовков: {dups}")
            lines += [f"- ⚠ {w}" for w in s.warnings]
            lines.append("")
    source = "лист" if r.settings_source == "sheet" else "по умолчанию"
    lines += ["## Настройки владельца", "", f"Источник: {source}", ""]
    lines += [f"- `{k}` = {v}" for k, v in r.settings_values.items()]
    lines += [f"- ⚠ {w}" for w in r.settings_warnings]
    if r.warnings:
        lines += ["", "## Предупреждения", *[f"- {w}" for w in r.warnings]]
    return "\n".join(lines) + "\n"


def to_json(r: CheckResult) -> str:
    return json.dumps(asdict(r), ensure_ascii=False, indent=2)


# ---------- задача ----------

def _docker_reminder(ctx: AppContext, now: datetime, days: int) -> str | None:
    last = from_db_time(ctx.db.get_meta(META_DOCKER_REMINDER))
    if last is not None and now - last < timedelta(days=days):
        return None
    ctx.db.set_meta(META_DOCKER_REMINDER, to_db_time(now))
    return ("напоминание (раз в 30 дней): очистите Docker на хосте — scripts\\docker-cleanup.ps1 "
            "(только висячие образы и кэш сборки, без -a и --volumes)")


def job(ctx: AppContext, trigger: str = "manual", as_json: bool = False, out: TextIO | None = None) -> JobResult:
    now = ctx.now()
    tz = ZoneInfo(ctx.settings.tz)
    stamp = now.astimezone(tz)
    started = stamp.strftime("%d.%m.%Y %H:%M МСК")

    try:
        contract = ctx.contract()
        rules = ctx.rules()
        retention = ctx.retention()
        ids = contract.resolve_ids(ctx.settings.env)
        reader = ctx.reader_factory(ctx.settings)
    except (ContractError, SettingsError, SettingsRulesError, ConfigError, SheetsAccessError) as exc:
        result = CheckResult(started_at=started, exit_code=EXIT_ACCESS, errors=[str(exc)])
    else:
        result = run_check(contract, ids, reader, rules=rules, started_at=started)
        result.warnings += size_warnings(ctx.settings, ctx.db, retention["limits"])
        reminder = _docker_reminder(ctx, now, int(retention.get("docker_cleanup_reminder_days", 30)))
        if reminder:
            result.warnings.append(reminder)
        if result.settings_source == "sheet":
            ctx.db.set_meta(META_USER_SETTINGS, json.dumps(result.settings_values, ensure_ascii=False))

    # отчёт-файл
    report_path = None
    try:
        ctx.settings.reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = ctx.settings.reports_dir / stamp.strftime("check_%Y%m%d_%H%M.md")
        report_path.write_text(render_markdown(result), encoding="utf-8")
    except OSError as exc:
        result.warnings.append(f"отчёт не сохранён ({type(exc).__name__})")

    if out is not None:
        print(to_json(result) if as_json else render_console(result), file=out)

    for e in result.errors:
        log.error("check: %s", e)
    for s in result.sheets:
        for w in s.warnings:
            log.warning("check: %s: %s", s.title, w)
    for w in result.warnings + result.settings_warnings:
        log.warning("check: %s", w)
    summary = _summary(result) + (f"; отчёт {report_path.name}" if report_path else "")
    log.info("check: %s", summary)

    has_warn = bool(result.warnings or result.settings_warnings or any(s.warnings for s in result.sheets))
    # код 1 (пропуски в таблице) — задача отработала, но есть проблемы → warn; код 2 (нет доступа) → error
    status = "error" if result.exit_code == EXIT_ACCESS else ("warn" if has_warn or result.exit_code else "ok")
    return JobResult(status, summary, result.exit_code, rows_read=result.rows_read)
