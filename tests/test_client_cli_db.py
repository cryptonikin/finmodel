import io
from contextlib import redirect_stdout
from datetime import timedelta

import pytest
import requests

from app.__main__ import main
from app.db import Database, to_db_time
from app.settings import REPO_ROOT
from app.sheets.client import READONLY_SCOPE, GoogleSheetsReader, SheetsAccessError, a1_sheet
from app.sheets.contract import load_contract
from tests.conftest import FakeSheetsReader, make_books


class Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def reader(responses):
    return GoogleSheetsReader(None, session=Session(responses), sleep=lambda s: None)


def test_scope_is_readonly_only():
    assert GoogleSheetsReader.scopes == (READONLY_SCOPE,)
    assert READONLY_SCOPE.endswith("spreadsheets.readonly")


def test_list_sheets_and_timeout():
    r = reader([Resp(200, {"sheets": [{"properties": {"title": "Журнал", "sheetId": 7,
                                                        "gridProperties": {"rowCount": 400, "columnCount": 20}}}]})])
    info = r.list_sheets("abc")
    assert info[0].title == "Журнал" and info[0].row_count == 400
    assert r._session.calls[0][2] == r.timeout


def test_batch_get_pads_and_retries():
    r = reader([Resp(503), requests.ConnectionError("x"),
                Resp(200, {"valueRanges": [{"values": [["a", 1]]}]})])
    out = r.batch_get("abc", ["'A'!1:1", "'B'!1:1"])
    assert out == [[["a", "1"]], []]
    assert r.requests_made == 3
    assert r.batch_get("abc", []) == []


@pytest.mark.parametrize("status, msg", [(403, "нет доступа"), (404, "не найдена"), (400, "HTTP 400")])
def test_errors_no_retry(status, msg):
    r = reader([Resp(status)])
    with pytest.raises(SheetsAccessError, match=msg) as e:
        r.list_sheets("SECRET-ID-123")
    assert "SECRET-ID-123" not in str(e.value)


def test_retries_exhausted_and_auth_error():
    with pytest.raises(SheetsAccessError, match="после 3 попыток"):
        reader([Resp(429), Resp(500), Resp(502)]).list_sheets("x")
    with pytest.raises(SheetsAccessError, match="авторизации"):
        reader([RuntimeError("refresh failed")]).list_sheets("x")


def test_bad_key_file(tmp_path):
    p = tmp_path / "google_sa"
    p.write_text('{"private_key": "nope"}')  # pragma: allowlist secret
    with pytest.raises(SheetsAccessError) as e:
        GoogleSheetsReader(p)
    assert "nope" not in str(e.value) and e.value.__cause__ is None


def test_a1_sheet():
    assert a1_sheet("Docker: настройки") == "'Docker: настройки'"
    assert a1_sheet("It's") == "'It''s'"


def test_db_runs(tmp_path, fixed_now):
    db = Database(tmp_path / "d" / "x.db")
    rid = db.start_run("check", "schedule", fixed_now)
    db.finish_run(rid, "ok", "s", 5, 0, fixed_now + timedelta(seconds=3))
    rid2 = db.start_run("check", "catch_up", fixed_now + timedelta(hours=1))
    db.finish_run(rid2, "error", "e", finished_at=fixed_now + timedelta(hours=1))
    assert db.last_success("check") == fixed_now
    assert db.last_started("check") == fixed_now + timedelta(hours=1)
    assert db.last_success("cleanup") is None
    with pytest.raises(ValueError):
        db.start_run("check", "cron")
    with pytest.raises(ValueError):
        db.finish_run(rid, "maybe")
    with pytest.raises(ValueError):
        to_db_time(fixed_now.replace(tzinfo=None))
    with pytest.raises(ValueError):
        db.delete_older("x; drop", "c", "1")
    with pytest.raises(ValueError):
        db.delete_orphans("a", "b", "c d", "e")
    assert db.table_exists("job_runs") and not db.table_exists("nope")
    db.close()


def test_cli_version_runs_check(ctx, capsys):
    assert main(["version"], ctx) == 0
    assert "0.1.1" in capsys.readouterr().out
    assert main(["runs"], ctx) == 0
    assert "ещё не было" in capsys.readouterr().out

    contract = load_contract(REPO_ROOT / "config" / "sheets_map.yaml")
    ctx.reader_factory = lambda s: FakeSheetsReader(make_books(contract))
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["check"], ctx) == 0
        assert main(["check", "--json"], ctx) == 0
        assert main(["cleanup", "--dry-run"], ctx) == 0
        assert main(["runs", "--last", "5"], ctx) == 0
    out = buf.getvalue()
    assert "Итог: код 0" in out and '"exit_code": 0' in out and "cleanup" in out


def test_cli_without_ctx(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FINMODEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FINMODEL_SECRETS_DIR", str(tmp_path / "nosecrets"))
    monkeypatch.setenv("SHEET_INVEST_ID", "a")
    monkeypatch.setenv("SHEET_BUDGET_ID", "b")
    assert main(["check"]) == 2
    assert "файл секрета не найден: google_sa" in capsys.readouterr().out
    monkeypatch.setenv("FINMODEL_LOG_FORMAT", "xml")
    assert main(["runs"]) == 2
    from app.logging_setup import setup_logging

    setup_logging()


def test_cli_scheduler_bad_config(ctx, tmp_path, capsys):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "schedule.yaml").write_text("timezone: Europe/Moscow\njobs: {bad: {at: ['07:30']}}\n")
    object.__setattr__(ctx.settings, "config_dir", cfg)
    assert main(["scheduler"], ctx) == 2
    assert "неизвестная задача" in capsys.readouterr().err
