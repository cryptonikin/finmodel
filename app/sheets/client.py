"""Доступ к Google Таблицам.

Ф0: только чтение (scope spreadsheets.readonly). Интерфейс записи SheetsWriter описан для Ф1+, но не реализован.
Таймауты на все запросы, ретраи с паузой на 429/5xx/сетевые ошибки. ID таблиц и ключи в тексты ошибок не попадают.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
API = "https://sheets.googleapis.com/v4/spreadsheets"

log = logging.getLogger(__name__)


class SheetsAccessError(Exception):
    """Нет доступа / сеть / API. Текст без ID и секретов."""


@dataclass(frozen=True)
class SheetInfo:
    title: str
    sheet_id: int
    row_count: int
    column_count: int


def a1_sheet(title: str) -> str:
    """Имя листа для A1-нотации: всегда в кавычках, апострофы удваиваются."""
    return "'" + title.replace("'", "''") + "'"


class SheetsReader(Protocol):
    def list_sheets(self, spreadsheet_id: str) -> list[SheetInfo]:
        """Листы таблицы с gridProperties (1 запрос)."""

    def batch_get(self, spreadsheet_id: str, ranges: Sequence[str]) -> list[list[list[str]]]:
        """Значения диапазонов (FORMATTED_VALUE, по строкам), 1 запрос на вызов."""


class SheetsWriter(Protocol):
    """Ф1+: запись. В Ф0 НЕ реализуется.

    Требования (TASK_F0 §11.2, TASK_F1 §7): отдельный scope и флаг; блокировка с Apps Script (лист-флаг) +
    файл-лок; бэкап листа (значения и формулы) перед первой записью; только values.batchUpdate по точным
    адресам (фильтры скрывают строки — copy/paste/autoFill не использовать); не добавлять строки/колонки.
    """

    def batch_update_values(self, spreadsheet_id: str, data: Sequence[tuple[str, list[list[Any]]]]) -> int:
        """Записывает значения в точные диапазоны одним запросом, возвращает число ячеек."""


class GoogleSheetsReader:
    """SheetsReader поверх REST API v4 с сервисным аккаунтом (только чтение)."""

    scopes = (READONLY_SCOPE,)

    def __init__(
        self,
        credentials_path: Path,
        *,
        timeout: tuple[float, float] = (10.0, 60.0),
        retries: int = 3,
        backoff: float = 2.0,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self.requests_made = 0
        if session is None:
            session = self._make_session(credentials_path)
        self._session = session

    def _make_session(self, credentials_path: Path) -> Any:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account

        try:
            creds = service_account.Credentials.from_service_account_file(
                str(credentials_path), scopes=list(self.scopes)
            )
        except Exception:  # noqa: BLE001 — исходный текст может содержать фрагменты ключа
            raise SheetsAccessError("ключ сервисного аккаунта не читается (google_sa)") from None
        return AuthorizedSession(creds)

    def _get(self, url: str, params: list[tuple[str, str]], what: str) -> dict[str, Any]:
        import requests

        last = "нет ответа"
        for attempt in range(1, self.retries + 1):
            self.requests_made += 1
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last = f"сеть: {type(exc).__name__}"
            except Exception as exc:  # noqa: BLE001 — ошибки получения токена google.auth
                raise SheetsAccessError(f"{what}: ошибка авторизации ({type(exc).__name__})") from None
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (401, 403):
                    raise SheetsAccessError(f"{what}: нет доступа (HTTP {resp.status_code}) — "
                                            "проверьте, что таблица открыта сервисному аккаунту")
                if resp.status_code == 404:
                    raise SheetsAccessError(f"{what}: таблица не найдена (HTTP 404) — проверьте ID в env")
                if resp.status_code != 429 and resp.status_code < 500:
                    raise SheetsAccessError(f"{what}: HTTP {resp.status_code}")
                last = f"HTTP {resp.status_code}"
            if attempt < self.retries:
                pause = self.backoff * (2 ** (attempt - 1))
                log.warning("%s: %s, повтор %d/%d через %.0f с", what, last, attempt, self.retries - 1, pause)
                self._sleep(pause)
        raise SheetsAccessError(f"{what}: {last} после {self.retries} попыток")

    def list_sheets(self, spreadsheet_id: str) -> list[SheetInfo]:
        data = self._get(
            f"{API}/{quote(spreadsheet_id, safe='')}",
            [("fields", "sheets.properties(sheetId,title,gridProperties(rowCount,columnCount))")],
            "список листов",
        )
        out = []
        for s in data.get("sheets", []):
            p = s.get("properties", {})
            g = p.get("gridProperties", {})
            out.append(SheetInfo(p.get("title", ""), int(p.get("sheetId", 0)),
                                 int(g.get("rowCount", 0)), int(g.get("columnCount", 0))))
        return out

    def batch_get(self, spreadsheet_id: str, ranges: Sequence[str]) -> list[list[list[str]]]:
        if not ranges:
            return []
        params = [("ranges", r) for r in ranges]
        params += [("majorDimension", "ROWS"), ("valueRenderOption", "FORMATTED_VALUE")]
        data = self._get(f"{API}/{quote(spreadsheet_id, safe='')}/values:batchGet", params, "чтение диапазонов")
        vrs = data.get("valueRanges", [])
        result = [[[str(c) for c in row] for row in vr.get("values", [])] for vr in vrs]
        while len(result) < len(ranges):
            result.append([])
        return result
