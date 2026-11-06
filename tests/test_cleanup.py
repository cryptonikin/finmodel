import io
import os
from datetime import timedelta
from pathlib import Path

import pytest

from app.jobs import cleanup
from app.jobs.cleanup import CleanupError, run_cleanup, safe_target, size_warnings
from app.scheduler import run_job


def touch(path: Path, age_days: float, now, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = (now - timedelta(days=age_days)).timestamp()
    os.utime(path, (t, t))
    return path


def counts(rep):
    return {i.name: i.count for i in rep.items}


def test_logs_reports_backups_by_age(ctx, fixed_now):
    s = ctx.settings
    old_log = touch(s.logs_dir / "finmodel.log.2026-09-01", 40, fixed_now)
    new_log = touch(s.logs_dir / "finmodel.log.2026-11-01", 5, fixed_now)
    cur_log = touch(s.logs_dir / "finmodel.log", 100, fixed_now)  # текущий файл не трогаем
    old_rep = touch(s.reports_dir / "check_20260901_0730.md", 31, fixed_now)
    new_rep = touch(s.reports_dir / "check_20261105_0730.md", 1, fixed_now)
    other = touch(s.reports_dir / "notes.txt", 400, fixed_now)  # не по шаблону
    backups = [touch(s.backups_dir / f"journal_{i:02d}.json", i, fixed_now) for i in range(12)]
    old_backup = touch(s.backups_dir / "journal_old.json", 31, fixed_now)

    rep = run_cleanup(ctx)
    c = counts(rep)
    assert c["логи /data/logs"] == 1 and not old_log.exists() and new_log.exists() and cur_log.exists()
    assert c["отчёты /data/reports"] == 1 and not old_rep.exists() and new_rep.exists() and other.exists()
    # оставлены 10 последних; 2 старших из 12 + 31-дневный удалены
    assert c["бэкапы /data/backups"] == 3
    assert [b.exists() for b in backups] == [True] * 10 + [False] * 2 and not old_backup.exists()


def test_backups_keep_min_3_even_if_old(ctx, fixed_now):
    # записей в «Журнал» не было больше месяца: все бэкапы старше 30 дней → последние 3 остаются
    files = [touch(ctx.settings.backups_dir / f"journal_{i:02d}.json", 40 + i, fixed_now) for i in range(5)]
    c = counts(run_cleanup(ctx))
    assert c["бэкапы /data/backups"] == 2
    assert [f.exists() for f in files] == [True, True, True, False, False]


def test_report_limit_100(ctx, fixed_now):
    files = [touch(ctx.settings.reports_dir / f"check_{i:03d}.md", i / 100, fixed_now) for i in range(105)]
    run_cleanup(ctx)
    assert sum(f.exists() for f in files) == 100
    assert not any(f.exists() for f in files[100:])  # удалены самые старые


def test_dry_run_deletes_nothing(ctx, fixed_now):
    f = touch(ctx.settings.logs_dir / "finmodel.log.old", 90, fixed_now)
    ctx.db.start_run("check", "manual", fixed_now - timedelta(days=200))
    out = io.StringIO()
    result = run_job(ctx, "cleanup", cleanup.job, dry_run=True, out=out)
    assert f.exists()
    assert "dry-run" in out.getvalue() and "dry-run" in result.summary
    assert len(ctx.db.recent_runs()) == 2  # старый запуск не удалён (+ текущий)
    assert not list(ctx.settings.reports_dir.glob("cleanup_*.md"))


def test_job_runs_retention_and_report(ctx, fixed_now):
    ctx.db.start_run("check", "manual", fixed_now - timedelta(days=91))
    ctx.db.start_run("check", "manual", fixed_now - timedelta(days=89))
    run_job(ctx, "cleanup", cleanup.job, out=io.StringIO())
    assert len(ctx.db.recent_runs()) == 2  # 89-дневный + сам cleanup
    rep = next(ctx.settings.reports_dir.glob("cleanup_*.md")).read_text(encoding="utf-8")
    assert "журнал запусков job_runs | 1 |" in rep and "VACUUM выполнен" in rep


def test_operations_window(ctx, fixed_now):
    db = ctx.db
    db.execute("CREATE TABLE tinvest_operations (op_id TEXT PRIMARY KEY, date_msk TEXT)")
    db.execute("CREATE TABLE journal_sync (op_id TEXT, status TEXT)")
    db.execute("CREATE TABLE tinvest_instruments (uid TEXT, last_used_at TEXT)")
    # окно по умолчанию 3 мес (93 дн) + 14 = 107 дней от 06.11.2026 → граница 22.07.2026
    for op, d in (("old", "2026-07-01"), ("edge", "2026-07-22"), ("inside", "2026-10-01")):
        db.execute("INSERT INTO tinvest_operations VALUES (?, ?)", (op, d))
        db.execute("INSERT INTO journal_sync VALUES (?, 'written')", (op,))
    db.execute("INSERT INTO tinvest_instruments VALUES ('a', ?)", ((fixed_now - timedelta(days=100)).isoformat(),))
    db.execute("INSERT INTO tinvest_instruments VALUES ('b', ?)", ((fixed_now - timedelta(days=10)).isoformat(),))

    dry = counts(run_cleanup(ctx, dry_run=True))
    assert dry["операции брокера tinvest_operations"] == 1 and dry["кэш инструментов"] == 1
    c = counts(run_cleanup(ctx))
    assert c["операции брокера tinvest_operations"] == 1 and c["journal_sync"] == 1 and c["кэш инструментов"] == 1
    left = db._conn.execute("SELECT op_id FROM tinvest_operations ORDER BY op_id").fetchall()
    assert [r[0] for r in left] == ["edge", "inside"]


def test_period_from_saved_settings(ctx):
    ctx.db.set_meta("user_settings.values", '{"tinvest.period_months": 1}')
    assert cleanup.period_months_from_meta(ctx.db, 3) == 1
    ctx.db.set_meta("user_settings.values", '{"tinvest.period_months": 9}')
    assert cleanup.period_months_from_meta(ctx.db, 3) == 3
    ctx.db.set_meta("user_settings.values", "мусор")
    assert cleanup.period_months_from_meta(ctx.db, 3) == 3


def test_path_outside_data_refused(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    link = root / "link.md"
    link.symlink_to(outside)
    with pytest.raises(CleanupError):
        safe_target(root / ".." / "secret.txt", root)
    with pytest.raises(CleanupError):
        safe_target(link, root)
    with pytest.raises(CleanupError):
        safe_target(root, root)
    with pytest.raises(CleanupError):
        safe_target(Path("/etc/passwd"), Path("/"))
    assert safe_target(root / "a.md", root) == (root / "a.md").resolve()


def test_size_warnings(ctx, fixed_now):
    touch(ctx.settings.logs_dir / "finmodel.log", 0, fixed_now, size=3 * 1024 * 1024)
    limits = {"db_mb": 0.00001, "data_mb": 1, "logs_per_day_mb": 1}
    w = " ".join(size_warnings(ctx.settings, ctx.db, limits))
    assert "БД" in w and "/data" in w and "логи за сутки" in w
