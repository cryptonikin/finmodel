"""Слой доступа к БД. Сейчас SQLite (/data/finmodel.db), позже Postgres на VPS.

Правило: весь SQL — здесь, только переносимые конструкции (без SQLite-трюков в остальном коде).
Время в БД — ISO-8601 в UTC (строка), сравнение строк = сравнение времени.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

TRIGGERS = ("schedule", "catch_up", "manual")
STATUSES = ("running", "ok", "warn", "error")

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS job_runs (
        run_id        TEXT PRIMARY KEY,
        job           TEXT NOT NULL,
        trigger       TEXT NOT NULL,
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        status        TEXT NOT NULL,
        summary       TEXT,
        rows_read     INTEGER NOT NULL DEFAULT 0,
        rows_written  INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_job_runs_job_started ON job_runs (job, started_at)",
    """
    CREATE TABLE IF NOT EXISTS meta (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    )
    """,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_db_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("ожидается время с часовым поясом")
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_db_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class JobRun:
    run_id: str
    job: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    summary: str
    rows_read: int
    rows_written: int


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self.migrate()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    def migrate(self) -> None:
        with self._tx() as c:
            for stmt in _SCHEMA:
                c.execute(stmt)

    # --- job_runs ---
    def start_run(self, job: str, trigger: str, started_at: datetime | None = None) -> str:
        if trigger not in TRIGGERS:
            raise ValueError(f"неизвестный trigger: {trigger}")
        run_id = uuid.uuid4().hex[:12]
        with self._tx() as c:
            c.execute(
                "INSERT INTO job_runs (run_id, job, trigger, started_at, status) VALUES (?, ?, ?, ?, ?)",
                (run_id, job, trigger, to_db_time(started_at or utcnow()), "running"),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        status: str,
        summary: str = "",
        rows_read: int = 0,
        rows_written: int = 0,
        finished_at: datetime | None = None,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"неизвестный status: {status}")
        with self._tx() as c:
            c.execute(
                "UPDATE job_runs SET finished_at = ?, status = ?, summary = ?, rows_read = ?, rows_written = ?"
                " WHERE run_id = ?",
                (to_db_time(finished_at or utcnow()), status, summary[:2000], rows_read, rows_written, run_id),
            )

    def _row(self, r: sqlite3.Row) -> JobRun:
        return JobRun(
            run_id=r["run_id"],
            job=r["job"],
            trigger=r["trigger"],
            started_at=from_db_time(r["started_at"]),  # type: ignore[arg-type]
            finished_at=from_db_time(r["finished_at"]),
            status=r["status"],
            summary=r["summary"] or "",
            rows_read=r["rows_read"],
            rows_written=r["rows_written"],
        )

    def recent_runs(self, limit: int = 20) -> list[JobRun]:
        rows = self._conn.execute(
            "SELECT * FROM job_runs ORDER BY started_at DESC, run_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def last_started(self, job: str, statuses: tuple[str, ...] | None = None) -> datetime | None:
        sql = "SELECT MAX(started_at) AS t FROM job_runs WHERE job = ?"
        params: list[object] = [job]
        if statuses:
            sql += f" AND status IN ({', '.join('?' for _ in statuses)})"
            params.extend(statuses)
        row = self._conn.execute(sql, params).fetchone()
        return from_db_time(row["t"]) if row else None

    def last_success(self, job: str) -> datetime | None:
        return self.last_started(job, ("ok", "warn"))

    def delete_runs_before(self, before: datetime, dry_run: bool = False) -> int:
        cutoff = to_db_time(before)
        if dry_run:
            return self._count("SELECT COUNT(*) FROM job_runs WHERE started_at < ?", (cutoff,))
        with self._tx() as c:
            return c.execute("DELETE FROM job_runs WHERE started_at < ?", (cutoff,)).rowcount

    # --- meta (служебные метки: последний VACUUM, напоминания) ---
    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM meta WHERE key = ?", (key,))
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, value))

    # --- обслуживание ---
    def _count(self, sql: str, params: tuple[object, ...] = ()) -> int:
        return int(self._conn.execute(sql, params).fetchone()[0])

    def table_exists(self, name: str) -> bool:
        return (
            self._count("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)) > 0
        )

    def delete_older(self, table: str, column: str, before: datetime | str, dry_run: bool = False) -> int:
        """Удаляет строки table, где column < before. Имена — только из кода (не из ввода)."""
        if not (table.isidentifier() and column.isidentifier()):
            raise ValueError("недопустимое имя таблицы/колонки")
        cutoff = to_db_time(before) if isinstance(before, datetime) else before
        if dry_run:
            return self._count(f"SELECT COUNT(*) FROM {table} WHERE {column} < ?", (cutoff,))  # noqa: S608
        with self._tx() as c:
            return c.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)).rowcount  # noqa: S608

    def delete_orphans(self, table: str, column: str, parent: str, parent_column: str, dry_run: bool = False) -> int:
        """Удаляет строки table, для которых нет parent.parent_column (journal_sync без операции)."""
        for n in (table, column, parent, parent_column):
            if not n.isidentifier():
                raise ValueError("недопустимое имя таблицы/колонки")
        where = f"{column} NOT IN (SELECT {parent_column} FROM {parent})"  # noqa: S608
        if dry_run:
            return self._count(f"SELECT COUNT(*) FROM {table} WHERE {where}")  # noqa: S608
        with self._tx() as c:
            return c.execute(f"DELETE FROM {table} WHERE {where}").rowcount  # noqa: S608

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def free_bytes(self) -> int:
        """Сколько байт файла БД свободно (после DELETE) — для решения о VACUUM. SQLite-специфично, только здесь."""
        pages = self._conn.execute("PRAGMA freelist_count").fetchone()[0]
        size = self._conn.execute("PRAGMA page_size").fetchone()[0]
        return int(pages) * int(size)

    def vacuum(self) -> None:
        self._conn.execute("VACUUM")

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        """Только для тестов и миграций Ф1+."""
        with self._tx() as c:
            c.execute(sql, params)
