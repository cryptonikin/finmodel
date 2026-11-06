"""Логи: консоль (text/json) + файл /data/logs/finmodel.log с ротацией по суткам.

Маскировка секретов — фильтром на каждом обработчике: сообщение, аргументы и текст исключения.
Формат: время МСК · уровень · задача · run_id · сообщение.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

MASK = "***"

# Порядок важен: сначала крупные блоки (PEM, private_key), затем токены и длинные base64.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN[^-]*-----.*?(?:-----END[^-]*-----|\Z)", re.DOTALL),
    re.compile(r"""(["']?private_key(?:_id)?["']?\s*[:=]\s*)(["'])(?:\\.|(?!\2).)*\2?""", re.DOTALL),
    re.compile(r"\bt\.[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)\b(bearer|token|password|secret|api[_-]?key)(\s*[:=]\s*|\s+)([^\s,;'\"]{8,})"),
    re.compile(r"[A-Za-z0-9+/_\-]{40,}={0,2}"),
)

_job_var: contextvars.ContextVar[str] = contextvars.ContextVar("job", default="-")
_run_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")


def mask_secrets(text: str) -> str:
    """Заменяет всё, похожее на токен/ключ, на ***."""
    if not text:
        return text
    out = _SECRET_PATTERNS[0].sub(MASK, text)
    out = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}{MASK}", out)
    out = _SECRET_PATTERNS[2].sub(MASK, out)
    out = _SECRET_PATTERNS[3].sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    out = _SECRET_PATTERNS[4].sub(MASK, out)
    return out


def looks_like_secret(text: str) -> bool:
    return bool(text) and mask_secrets(text) != text


@contextmanager
def log_context(job: str = "-", run_id: str = "-") -> Iterator[None]:
    t1, t2 = _job_var.set(job), _run_var.set(run_id)
    try:
        yield
    finally:
        _job_var.reset(t1)
        _run_var.reset(t2)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.job = _job_var.get()
        record.run_id = _run_var.get()
        return True


class SecretMaskingFilter(logging.Filter):
    """Маскирует сообщение и исключение до форматирования."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — кривые аргументы не должны ронять логирование
            message = str(record.msg)
        record.msg = mask_secrets(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = mask_secrets(record.exc_text)
        if record.stack_info:
            record.stack_info = mask_secrets(record.stack_info)
        return True


class _TzMixin:
    tz: ZoneInfo

    def _time(self, record: logging.LogRecord) -> str:
        return datetime.fromtimestamp(record.created, self.tz).strftime("%Y-%m-%d %H:%M:%S")


class TextFormatter(_TzMixin, logging.Formatter):
    def __init__(self, tz: str) -> None:
        super().__init__()
        self.tz = ZoneInfo(tz)

    def format(self, record: logging.LogRecord) -> str:
        job = getattr(record, "job", "-")
        run_id = getattr(record, "run_id", "-")
        line = f"{self._time(record)} · {record.levelname} · {job} · {run_id} · {record.getMessage()}"
        if record.exc_text:
            line += "\n" + record.exc_text
        return line


class JsonFormatter(_TzMixin, logging.Formatter):
    def __init__(self, tz: str) -> None:
        super().__init__()
        self.tz = ZoneInfo(tz)

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "time": self._time(record),
            "level": record.levelname,
            "job": getattr(record, "job", "-"),
            "run_id": getattr(record, "run_id", "-"),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_text:
            data["exc"] = record.exc_text
        return json.dumps(data, ensure_ascii=False)


def _prepare(handler: logging.Handler, formatter: logging.Formatter) -> logging.Handler:
    handler.addFilter(ContextFilter())
    handler.addFilter(SecretMaskingFilter())
    handler.setFormatter(formatter)
    return handler


def setup_logging(
    *,
    tz: str = "Europe/Moscow",
    console_format: str = "text",
    logs_dir: Path | None = None,
    backup_days: int = 30,
    level: int = logging.INFO,
) -> None:
    """Настраивает корневой логгер. Повторный вызов заменяет обработчики."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(level)

    console_fmt = JsonFormatter(tz) if console_format == "json" else TextFormatter(tz)
    root.addHandler(_prepare(logging.StreamHandler(sys.stderr), console_fmt))

    if logs_dir is not None:
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            fh = TimedRotatingFileHandler(
                logs_dir / "finmodel.log", when="midnight", backupCount=backup_days, encoding="utf-8"
            )
        except OSError as exc:
            root.warning("файловый лог недоступен (%s), пишу только в консоль", type(exc).__name__)
        else:
            root.addHandler(_prepare(fh, TextFormatter(tz)))

    for noisy in ("urllib3", "google", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
