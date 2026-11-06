from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.jobs import JobResult
from app.scheduler import (
    EXIT_BUSY,
    JobSchedule,
    Schedule,
    ScheduleError,
    Scheduler,
    apply_overrides,
    decide,
    due_jobs,
    job_lock,
    last_slot,
    parse_schedule,
    run_job,
)

MSK = ZoneInfo("Europe/Moscow")
TZ = "Europe/Moscow"
CHECK = JobSchedule("check", (time(7, 30),), None, True)


def msk(*a):
    return datetime(*a, tzinfo=MSK)


def test_last_slot_moscow_time():
    # 04:31 UTC = 07:31 МСК → слот сегодня 07:30
    now = datetime(2026, 11, 6, 4, 31, tzinfo=UTC)
    assert last_slot(CHECK, now, TZ) == msk(2026, 11, 6, 7, 30)
    # 04:29 UTC = 07:29 МСК → вчерашний слот
    assert last_slot(CHECK, datetime(2026, 11, 6, 4, 29, tzinfo=UTC), TZ) == msk(2026, 11, 5, 7, 30)


def test_last_slot_weekdays_and_multiple_times():
    job = JobSchedule("x", (time(9, 0), time(18, 0)), frozenset({0}), True)  # только понедельник
    # пятница 06.11.2026 → последний — понедельник 02.11 18:00
    assert last_slot(job, msk(2026, 11, 6, 12, 0), TZ) == msk(2026, 11, 2, 18, 0)
    assert last_slot(job, msk(2026, 11, 9, 12, 0), TZ) == msk(2026, 11, 9, 9, 0)
    never = JobSchedule("y", (time(9, 0),), frozenset(), True)
    assert last_slot(never, msk(2026, 11, 6, 12, 0), TZ) is None


def test_on_time_run():
    assert decide(CHECK, msk(2026, 11, 6, 7, 30, 30), TZ, msk(2026, 11, 5, 7, 30), None) == "schedule"


def test_catch_up_once_after_laptop_off():
    # ноутбук выключен 3 дня, включили в 12:00 → один догон
    last_ok = msk(2026, 11, 3, 7, 30, 5)
    now = msk(2026, 11, 6, 12, 0)
    assert decide(CHECK, now, TZ, last_ok, last_ok) == "catch_up"
    # после успешного догона в 12:00 — больше не положено до завтрашних 07:30
    done = msk(2026, 11, 6, 12, 0, 10)
    for minutes in (1, 60, 600):
        assert decide(CHECK, now + timedelta(minutes=minutes), TZ, done, done) is None
    assert decide(CHECK, msk(2026, 11, 7, 7, 30), TZ, done, done) == "schedule"


def test_no_catch_up_when_disabled():
    job = JobSchedule("x", (time(7, 30),), None, False)
    assert decide(job, msk(2026, 11, 6, 12, 0), TZ, msk(2026, 11, 5, 7, 31), None) is None


def test_retry_after_error_not_every_minute():
    attempt = msk(2026, 11, 6, 7, 30, 2)
    assert decide(CHECK, msk(2026, 11, 6, 7, 31), TZ, None, attempt) is None
    assert decide(CHECK, msk(2026, 11, 6, 8, 5), TZ, None, attempt) == "catch_up"


def test_due_jobs_no_double_run(ctx):
    calls = []

    def fake_job(c, trigger="manual", **_):
        calls.append(trigger)
        return JobResult("ok", "готово")

    sch = Scheduler.__new__(Scheduler)
    sch.ctx, sch.jobs = ctx, {"check": fake_job}
    sch.base = Schedule(TZ, {"check": CHECK})
    import threading

    sch.stop = threading.Event()
    times = iter([msk(2026, 11, 6, 12, 0), msk(2026, 11, 6, 12, 0, 5), msk(2026, 11, 6, 12, 1),
                  msk(2026, 11, 6, 12, 1, 5), msk(2026, 11, 6, 12, 2)])
    ctx.now = lambda: next(times)
    assert sch.tick() == [("check", "catch_up")]
    assert sch.tick() == []
    assert calls == ["catch_up"]
    assert ctx.db.recent_runs()[0].trigger == "catch_up"


def test_due_jobs_pure():
    sched = Schedule(TZ, {"check": CHECK, "cleanup": JobSchedule("cleanup", (time(21, 45),), None, True)})
    hist = {"check": (msk(2026, 11, 6, 7, 30, 3),) * 2, "cleanup": (None, None)}
    assert due_jobs(sched, msk(2026, 11, 6, 12, 0), lambda n: hist[n]) == [("cleanup", "catch_up")]


def test_parse_schedule_and_overrides():
    s = parse_schedule({"timezone": TZ, "jobs": {"check": {"at": ["07:30"], "days": ["mon", "fri"]}}}, {"check"})
    assert s.jobs["check"].weekdays == frozenset({0, 4}) and s.jobs["check"].catch_up
    s2 = apply_overrides(s, {"schedule.check": "08:15", "schedule.unknown": "01:00", "write.dry_run": True})
    assert s2.jobs["check"].at == (time(8, 15),)
    assert apply_overrides(s, {"schedule.check": "25:00"}).jobs["check"].at == (time(7, 30),)


@pytest.mark.parametrize("data", [
    {"timezone": "Mars/Base", "jobs": {}},
    {"timezone": TZ},
    {"timezone": TZ, "jobs": {"nope": {"at": ["07:30"]}}},
    {"timezone": TZ, "jobs": {"check": {"at": ["7:3"]}}},
    {"timezone": TZ, "jobs": {"check": {"at": []}}},
    {"timezone": TZ, "jobs": {"check": {"at": ["07:30"], "days": "weekly"}}},
    {"timezone": TZ, "jobs": {"check": {"at": ["07:30"], "catch_up": "yes"}}},
])
def test_parse_schedule_errors(data):
    with pytest.raises(ScheduleError):
        parse_schedule(data, {"check"})


def test_repo_schedule_loads(ctx):
    sch = Scheduler(ctx)
    assert set(sch.base.jobs) == {"check", "cleanup"}
    ctx.db.set_meta("user_settings.values", '{"schedule.cleanup": "22:00"}')
    assert sch.schedule().jobs["cleanup"].at == (time(22, 0),)
    ctx.db.set_meta("user_settings.values", "не json")
    assert sch.schedule().jobs["cleanup"].at == (time(21, 45),)


def test_lock_prevents_parallel_jobs(ctx):
    def fake(c, trigger="manual", **_):
        return JobResult("ok", "x")

    with job_lock(ctx.settings.data_dir):
        r = run_job(ctx, "check", fake)
    assert r.exit_code == EXIT_BUSY
    assert ctx.db.recent_runs() == []
    assert run_job(ctx, "check", fake).status == "ok"


def test_job_exception_recorded(ctx):
    def boom(c, trigger="manual", **_):
        raise RuntimeError("t.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    r = run_job(ctx, "check", boom)
    assert r.status == "error"
    run = ctx.db.recent_runs()[0]
    assert run.status == "error" and "RuntimeError" in run.summary and "t.AAA" not in run.summary


def test_run_stops_on_signal(ctx, monkeypatch):
    sch = Scheduler(ctx, jobs={"check": lambda c, **_: JobResult("ok", ""),
                               "cleanup": lambda c, **_: JobResult("ok", "")})
    ctx.db.start_run("check", "manual", ctx.now())
    monkeypatch.setattr("app.scheduler.signal.signal", lambda *a: None)
    original = sch.tick

    def tick_then_stop():
        out = original()
        sch._on_signal(15, None)
        return out

    sch.tick = tick_then_stop
    assert sch.run() == 0
