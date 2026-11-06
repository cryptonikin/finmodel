"""CLI: python -m app {check|scheduler|runs|cleanup|version}."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from app import __version__
from app.context import AppContext, ConfigError
from app.db import Database
from app.logging_setup import setup_logging
from app.scheduler import ScheduleError, Scheduler, run_job
from app.settings import Settings, SettingsError


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app", description="finmodel — сервис финмодели")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="проверка таблиц (только чтение)")
    c.add_argument("--json", action="store_true", help="вывод в JSON")
    sub.add_parser("scheduler", help="фоновый планировщик с догоном")
    r = sub.add_parser("runs", help="последние запуски задач")
    r.add_argument("--last", type=int, default=20)
    cl = sub.add_parser("cleanup", help="очистка локальных данных по срокам")
    cl.add_argument("--dry-run", action="store_true", help="только показать, ничего не удалять")
    sub.add_parser("version", help="версия")
    return p


def _print_runs(ctx: AppContext, last: int) -> None:
    tz = ZoneInfo(ctx.settings.tz)
    runs = ctx.db.recent_runs(last)
    if not runs:
        print("запусков ещё не было")
        return
    print(f"{'начало (МСК)':<17} {'задача':<10} {'запуск':<9} {'статус':<7} {'сек':>5} {'чтен':>6} {'запис':>6}  итог")
    for r in runs:
        dur = f"{(r.finished_at - r.started_at).total_seconds():.0f}" if r.finished_at else "-"
        print(f"{r.started_at.astimezone(tz):%d.%m.%Y %H:%M}  {r.job:<10} {r.trigger:<9} {r.status:<7} "
              f"{dur:>5} {r.rows_read:>6} {r.rows_written:>6}  {r.summary}")


def main(argv: Sequence[str] | None = None, ctx: AppContext | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "version":
        print(f"finmodel {__version__}")
        return 0

    if ctx is None:
        try:
            settings = Settings.from_env()
        except SettingsError as exc:
            print(f"ошибка настроек: {exc}", file=sys.stderr)
            return 2
        setup_logging(tz=settings.tz, console_format=settings.log_format, logs_dir=settings.logs_dir)
        try:
            ctx = AppContext(settings=settings, db=Database(settings.db_path))
        except OSError as exc:
            print(f"нет доступа к каталогу данных ({type(exc).__name__})", file=sys.stderr)
            return 2

    if args.command == "runs":
        _print_runs(ctx, args.last)
        return 0
    if args.command == "check":
        from app.jobs import check

        return run_job(ctx, "check", check.job, "manual", as_json=args.json, out=sys.stdout).exit_code
    if args.command == "cleanup":
        from app.jobs import cleanup

        return run_job(ctx, "cleanup", cleanup.job, "manual", dry_run=args.dry_run, out=sys.stdout).exit_code
    if args.command == "scheduler":
        try:
            return Scheduler(ctx).run()
        except (ScheduleError, ConfigError) as exc:
            print(f"ошибка расписания: {exc}", file=sys.stderr)
            return 2
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
