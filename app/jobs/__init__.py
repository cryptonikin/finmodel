"""Задачи сервиса. Каждая задача: job(ctx, trigger) -> JobResult."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class JobResult:
    status: str  # ok | warn | error
    summary: str
    exit_code: int = 0
    rows_read: int = 0
    rows_written: int = 0
