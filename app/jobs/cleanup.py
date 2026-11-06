"""Задача cleanup (TASK_F0 §13): локально храним только нужное для работы и дедупа.

Удаляет только внутри data_dir (/data). Сроки — config/retention.yaml. --dry-run ничего не удаляет и не пишет.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.context import AppContext
from app.db import Database, from_db_time, to_db_time
from app.jobs import JobResult
from app.settings import Settings

log = logging.getLogger(__name__)

META_LAST_VACUUM = "cleanup.last_vacuum_at"
META_USER_SETTINGS = "user_settings.values"
MB = 1024 * 1024


class CleanupError(Exception):
    pass


def safe_target(path: Path, root: Path) -> Path:
    """Путь внутри root (после разрешения ссылок), иначе отказ."""
    root_r = root.resolve()
    target = path.resolve()
    if root_r == Path(root_r.anchor) or target == root_r or not target.is_relative_to(root_r):
        raise CleanupError(f"отказ: путь вне каталога данных: {path}")
    return target


@dataclass
class CleanupItem:
    name: str
    count: int
    note: str = ""


@dataclass
class CleanupReport:
    dry_run: bool
    items: list[CleanupItem] = field(default_factory=list)
    db_before: int = 0
    db_after: int = 0
    vacuumed: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(i.count for i in self.items)


def _files(directory: Path, patterns: list[str]) -> list[Path]:
    if not directory.is_dir():
        return []
    found: set[Path] = set()
    for p in patterns:
        found.update(f for f in directory.glob(p) if f.is_file() and not f.is_symlink())
    return sorted(found, key=lambda f: f.stat().st_mtime, reverse=True)  # новые первыми


def _delete(files: list[Path], root: Path, dry_run: bool) -> int:
    for f in files:
        target = safe_target(f, root)
        if not dry_run:
            target.unlink(missing_ok=True)
    return len(files)


def clean_logs(settings: Settings, now: datetime, days: int, dry_run: bool) -> int:
    cutoff = (now - timedelta(days=days)).timestamp()
    old = [f for f in _files(settings.logs_dir, ["finmodel.log.*"]) if f.stat().st_mtime < cutoff]
    return _delete(old, settings.data_dir, dry_run)


def clean_reports(settings: Settings, now: datetime, cfg: dict[str, Any], dry_run: bool) -> int:
    files = _files(settings.reports_dir, list(cfg.get("patterns", ["*.md"])))
    cutoff = (now - timedelta(days=int(cfg["days"]))).timestamp()
    keep = [f for f in files if f.stat().st_mtime >= cutoff]
    drop = [f for f in files if f.stat().st_mtime < cutoff]
    max_files = int(cfg["max_files"])
    drop += keep[max_files:]
    return _delete(drop, settings.data_dir, dry_run)


def clean_backups(settings: Settings, now: datetime, cfg: dict[str, Any], dry_run: bool) -> int:
    files = _files(settings.backups_dir, list(cfg.get("patterns", ["*.json"])))
    cutoff = (now - timedelta(days=int(cfg["days"]))).timestamp()
    keep_last = int(cfg["keep_last"])
    drop = [f for i, f in enumerate(files) if i >= keep_last or f.stat().st_mtime < cutoff]
    return _delete(drop, settings.data_dir, dry_run)


def clean_operations(db: Database, now: datetime, tz: str, period_months: int, extra_days: int,
                     dry_run: bool) -> tuple[int, int]:
    """tinvest_operations старше окна загрузки + extra_days; journal_sync без операции. Таблицы — из Ф1."""
    if not db.table_exists("tinvest_operations"):
        return 0, 0
    window_days = period_months * 31 + extra_days
    cutoff = (now.astimezone(ZoneInfo(tz)) - timedelta(days=window_days)).date().isoformat()
    ops = db.delete_older("tinvest_operations", "date_msk", cutoff, dry_run)
    sync = 0
    if db.table_exists("journal_sync"):
        if dry_run:
            # в dry-run операции не удалены — считаем sync, который осиротел бы (приближённо: уже осиротевшие)
            sync = db.delete_orphans("journal_sync", "op_id", "tinvest_operations", "op_id", dry_run=True)
        else:
            sync = db.delete_orphans("journal_sync", "op_id", "tinvest_operations", "op_id")
    return ops, sync


def clean_instruments(db: Database, now: datetime, unused_days: int, dry_run: bool) -> int:
    if not db.table_exists("tinvest_instruments"):
        return 0
    return db.delete_older("tinvest_instruments", "last_used_at", now - timedelta(days=unused_days), dry_run)


def dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def size_warnings(settings: Settings, db: Database, limits: dict[str, Any]) -> list[str]:
    out = []
    db_size = db.size_bytes()
    if db_size > float(limits["db_mb"]) * MB:
        out.append(f"БД {db_size / MB:.0f} МБ > {limits['db_mb']} МБ")
    data = dir_size(settings.data_dir)
    if data > float(limits["data_mb"]) * MB:
        out.append(f"/data {data / MB:.0f} МБ > {limits['data_mb']} МБ")
    current_log = settings.logs_dir / "finmodel.log"
    if current_log.is_file() and current_log.stat().st_size > float(limits["logs_per_day_mb"]) * MB:
        out.append(f"логи за сутки {current_log.stat().st_size / MB:.0f} МБ > {limits['logs_per_day_mb']} МБ")
    return out


def period_months_from_meta(db: Database, default: int) -> int:
    raw = db.get_meta(META_USER_SETTINGS)
    try:
        value = int(json.loads(raw)["tinvest.period_months"]) if raw else default
    except (ValueError, KeyError, TypeError):
        value = default
    return value if 1 <= value <= 3 else default


def run_cleanup(ctx: AppContext, dry_run: bool = False) -> CleanupReport:
    s, db, now = ctx.settings, ctx.db, ctx.now()
    cfg = ctx.retention()
    rep = CleanupReport(dry_run=dry_run, db_before=db.size_bytes())

    rep.items.append(CleanupItem("логи /data/logs", clean_logs(s, now, int(cfg["logs"]["days"]), dry_run),
                                 f"старше {cfg['logs']['days']} дн"))
    rep.items.append(CleanupItem("журнал запусков job_runs",
                                 db.delete_runs_before(now - timedelta(days=int(cfg["job_runs"]["days"])), dry_run),
                                 f"старше {cfg['job_runs']['days']} дн"))
    rep.items.append(CleanupItem("отчёты /data/reports", clean_reports(s, now, cfg["reports"], dry_run),
                                 f"старше {cfg['reports']['days']} дн или сверх {cfg['reports']['max_files']}"))
    rep.items.append(CleanupItem("бэкапы /data/backups", clean_backups(s, now, cfg["backups"], dry_run),
                                 f"кроме последних {cfg['backups']['keep_last']}"
                                 f" не старше {cfg['backups']['days']} дн"))
    period = period_months_from_meta(db, int(ctx.rules()["tinvest.period_months"].default))
    ops, sync = clean_operations(db, now, s.tz, period, int(cfg["tinvest_operations"]["extra_days"]), dry_run)
    rep.items.append(CleanupItem("операции брокера tinvest_operations", ops,
                                 f"окно {period} мес + {cfg['tinvest_operations']['extra_days']} дн"))
    rep.items.append(CleanupItem("journal_sync", sync, "вместе с операциями"))
    rep.items.append(CleanupItem("кэш инструментов", clean_instruments(
        db, now, int(cfg["instruments_cache"]["unused_days"]), dry_run),
        f"не использовался {cfg['instruments_cache']['unused_days']} дн"))

    if not dry_run:
        size_now = db.size_bytes()
        last_vac = from_db_time(db.get_meta(META_LAST_VACUUM))
        # SQLite не уменьшает файл при DELETE: оцениваем освобождённое по свободным страницам
        freed = db.free_bytes()
        need = (size_now and freed / size_now > float(cfg["vacuum"]["freed_ratio"])) or (
            last_vac is None or now - last_vac > timedelta(days=int(cfg["vacuum"]["every_days"])))
        if need:
            db.vacuum()
            db.set_meta(META_LAST_VACUUM, to_db_time(now))
            rep.vacuumed = True
    rep.db_after = db.size_bytes()
    rep.warnings = size_warnings(s, db, cfg["limits"])
    return rep


def render_markdown(rep: CleanupReport, now: datetime, tz: str) -> str:
    t = now.astimezone(ZoneInfo(tz)).strftime("%d.%m.%Y %H:%M")
    lines = [f"# Очистка данных — {t} МСК", ""]
    if rep.dry_run:
        lines += ["**Режим dry-run: ничего не удалено.**", ""]
    lines += ["| Что | Удалено | Правило |", "|---|---:|---|"]
    lines += [f"| {i.name} | {i.count} | {i.note} |" for i in rep.items]
    lines += ["", f"Размер БД: до {rep.db_before / MB:.2f} МБ, после {rep.db_after / MB:.2f} МБ"
              + (" (VACUUM выполнен)" if rep.vacuumed else ""), ""]
    if rep.warnings:
        lines += ["## Предупреждения", *[f"- {w}" for w in rep.warnings], ""]
    return "\n".join(lines)


def job(ctx: AppContext, trigger: str = "manual", dry_run: bool = False, out: Any = None) -> JobResult:
    rep = run_cleanup(ctx, dry_run=dry_run)
    now = ctx.now()
    text = render_markdown(rep, now, ctx.settings.tz)
    if out is not None:
        print(text, file=out)
    if not dry_run:
        ctx.settings.reports_dir.mkdir(parents=True, exist_ok=True)
        name = now.astimezone(ZoneInfo(ctx.settings.tz)).strftime("cleanup_%Y%m%d_%H%M.md")
        (ctx.settings.reports_dir / name).write_text(text, encoding="utf-8")
    for w in rep.warnings:
        log.warning("размер: %s", w)
    summary = f"удалено {rep.total}{' (dry-run)' if dry_run else ''}; БД {rep.db_after / MB:.2f} МБ"
    log.info("cleanup: %s", summary)
    return JobResult("warn" if rep.warnings else "ok", summary, 0)
