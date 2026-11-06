"""Планировщик внутри контейнера с догоном пропущенных задач (TASK_F0 §6).

- Раз в минуту: какие задачи положены (чистые функции last_slot/decide/due_jobs — тестируются с фикс. временем).
- Последний успешный запуск — SQLite job_runs. Пропущенный слот (ноутбук был выключен) выполняется ОДИН раз,
  сколько бы слотов ни было пропущено.
- Одновременно одна задача: последовательный цикл + файл-лок /data/finmodel.lock (общий с разовыми командами).
- Корректная остановка по SIGTERM/SIGINT.
"""

from __future__ import annotations

import fcntl
import json
import logging
import re
import signal
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.context import AppContext, load_yaml
from app.jobs import JobResult
from app.jobs.cleanup import META_USER_SETTINGS
from app.logging_setup import log_context

log = logging.getLogger(__name__)

GRACE = timedelta(minutes=5)  # опоздание в пределах — обычный запуск, дальше — догон
RETRY_AFTER = timedelta(minutes=30)  # после ошибки повтор не раньше
LOOKBACK_DAYS = 8
EXIT_BUSY = 3
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

JobFunc = Callable[..., JobResult]


class ScheduleError(Exception):
    pass


@dataclass(frozen=True)
class JobSchedule:
    name: str
    at: tuple[time, ...]
    weekdays: frozenset[int] | None = None  # None — ежедневно
    catch_up: bool = True


@dataclass(frozen=True)
class Schedule:
    timezone: str
    jobs: Mapping[str, JobSchedule]


def _parse_time(value: object, where: str) -> time:
    m = _HHMM.match(str(value).strip())
    if not m:
        raise ScheduleError(f"{where}: время должно быть ЧЧ:ММ")
    return time(int(m.group(1)), int(m.group(2)))


def parse_schedule(data: Mapping[str, Any], known_jobs: set[str]) -> Schedule:
    tz = data.get("timezone")
    try:
        ZoneInfo(str(tz))
    except Exception:  # noqa: BLE001
        raise ScheduleError("schedule: неизвестный timezone") from None
    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, dict):
        raise ScheduleError("schedule: нет раздела jobs")
    jobs: dict[str, JobSchedule] = {}
    for name, spec in raw_jobs.items():
        if name not in known_jobs:
            raise ScheduleError(f"schedule: неизвестная задача {name}")
        if not isinstance(spec, dict) or not isinstance(spec.get("at"), list) or not spec["at"]:
            raise ScheduleError(f"schedule.{name}: нужен список at")
        at = tuple(sorted(_parse_time(t, f"schedule.{name}.at") for t in spec["at"]))
        days = spec.get("days", "daily")
        if days == "daily":
            weekdays = None
        elif isinstance(days, list) and days and all(d in _WEEKDAYS for d in days):
            weekdays = frozenset(_WEEKDAYS[d] for d in days)
        else:
            raise ScheduleError(f"schedule.{name}.days: daily или список mon…sun")
        catch_up = spec.get("catch_up", True)
        if not isinstance(catch_up, bool):
            raise ScheduleError(f"schedule.{name}.catch_up: true/false")
        jobs[name] = JobSchedule(name, at, weekdays, catch_up)
    return Schedule(str(tz), jobs)


def apply_overrides(schedule: Schedule, user_values: Mapping[str, Any]) -> Schedule:
    """«schedule.<задача>» = ЧЧ:ММ из листа настроек заменяет время задачи (только для известных задач)."""
    jobs = dict(schedule.jobs)
    for key, value in user_values.items():
        if not key.startswith("schedule."):
            continue
        name = key.removeprefix("schedule.")
        if name in jobs:
            try:
                jobs[name] = replace(jobs[name], at=(_parse_time(value, key),))
            except ScheduleError:
                log.warning("настройка %s: неверное время — оставлено из schedule.yaml", key)
    return Schedule(schedule.timezone, jobs)


def last_slot(job: JobSchedule, now: datetime, tz: str) -> datetime | None:
    """Последний положенный слот ≤ now (в часовом поясе расписания), с tzinfo."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    for back in range(LOOKBACK_DAYS):
        day = local.date() - timedelta(days=back)
        if job.weekdays is not None and day.weekday() not in job.weekdays:
            continue
        for t in reversed(job.at):
            slot = datetime.combine(day, t, tzinfo=zone)
            if slot <= local:
                return slot
    return None


def decide(
    job: JobSchedule,
    now: datetime,
    tz: str,
    last_success: datetime | None,
    last_attempt: datetime | None,
) -> str | None:
    """Нужно ли запускать сейчас: 'schedule' | 'catch_up' | None."""
    slot = last_slot(job, now, tz)
    if slot is None:
        return None
    if last_success is not None and last_success >= slot:
        return None
    if last_attempt is not None and last_attempt >= slot and now - last_attempt < RETRY_AFTER:
        return None
    if now - slot <= GRACE:
        return "schedule"
    return "catch_up" if job.catch_up else None


def due_jobs(
    schedule: Schedule,
    now: datetime,
    history: Callable[[str], tuple[datetime | None, datetime | None]],
) -> list[tuple[str, str]]:
    out = []
    for name, job in schedule.jobs.items():
        last_success, last_attempt = history(name)
        trigger = decide(job, now, schedule.timezone, last_success, last_attempt)
        if trigger:
            out.append((name, trigger))
    return out


# ---------- выполнение ----------

class LockBusy(Exception):
    pass


@contextmanager
def job_lock(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / "finmodel.lock", "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusy("другая задача уже выполняется") from None
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def run_job(ctx: AppContext, name: str, func: JobFunc, trigger: str = "manual", **kwargs: Any) -> JobResult:
    """Запуск задачи: блокировка, запись в job_runs, контекст логов, перехват исключений."""
    try:
        with job_lock(ctx.settings.data_dir):
            run_id = ctx.db.start_run(name, trigger, ctx.now())
            with log_context(name, run_id):
                log.info("старт (%s)", trigger)
                try:
                    result = func(ctx, trigger=trigger, **kwargs)
                except Exception as exc:
                    log.exception("задача упала: %s", type(exc).__name__)
                    result = JobResult("error", f"исключение {type(exc).__name__}", 2)
                ctx.db.finish_run(run_id, result.status, result.summary, result.rows_read, result.rows_written,
                                  ctx.now())
                log.info("финиш: %s", result.status)
                return result
    except LockBusy as exc:
        log.warning("%s: %s — пропуск", name, exc)
        return JobResult("error", str(exc), EXIT_BUSY)


def default_jobs() -> dict[str, JobFunc]:
    from app.jobs import check, cleanup

    return {"check": check.job, "cleanup": cleanup.job}


class Scheduler:
    def __init__(self, ctx: AppContext, jobs: Mapping[str, JobFunc] | None = None) -> None:
        self.ctx = ctx
        self.jobs = dict(jobs or default_jobs())
        self.base = parse_schedule(load_yaml(ctx.settings.config_dir / "schedule.yaml"), set(self.jobs))
        self.stop = threading.Event()

    def schedule(self) -> Schedule:
        raw = self.ctx.db.get_meta(META_USER_SETTINGS)
        try:
            values = json.loads(raw) if raw else {}
        except ValueError:
            values = {}
        return apply_overrides(self.base, values)

    def _history(self, name: str) -> tuple[datetime | None, datetime | None]:
        return self.ctx.db.last_success(name), self.ctx.db.last_started(name)

    def tick(self) -> list[tuple[str, str]]:
        due = due_jobs(self.schedule(), self.ctx.now(), self._history)
        for name, trigger in due:
            if self.stop.is_set():
                break
            run_job(self.ctx, name, self.jobs[name], trigger)
        return due

    def _on_signal(self, signum: int, _frame: object) -> None:
        log.info("получен сигнал %s — останавливаюсь после текущей задачи", signal.Signals(signum).name)
        self.stop.set()

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        log.info("планировщик запущен: %s", ", ".join(
            f"{j.name} {'/'.join(t.strftime('%H:%M') for t in j.at)}" for j in self.schedule().jobs.values()))
        while not self.stop.is_set():
            self.tick()
            now = self.ctx.now()
            self.stop.wait(max(1.0, 60 - now.second - now.microsecond / 1e6))
        log.info("планировщик остановлен")
        return 0
