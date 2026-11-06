import io
import json

from app.jobs import check
from app.jobs.check import EXIT_ACCESS, EXIT_MISSING, EXIT_OK, run_check
from app.scheduler import run_job
from app.settings import REPO_ROOT
from app.sheets.contract import load_contract
from tests.conftest import FakeSheetsReader, make_books

CONTRACT = load_contract(REPO_ROOT / "config" / "sheets_map.yaml")
IDS = {"invest": "fake-invest", "budget": "fake-budget"}


def _run(books, **kw):
    reader = FakeSheetsReader(books, **{k: v for k, v in kw.items() if k in ("grids", "fail")})
    return run_check(CONTRACT, IDS, reader, rules=kw.get("rules")), reader


def _sheet(res, alias):
    return next(s for s in res.sheets if s.alias == alias)


def test_all_found_and_request_budget():
    res, reader = _run(make_books(CONTRACT))
    assert res.exit_code == EXIT_OK, check.render_console(res)
    assert res.requests <= 4 and len(reader.calls) == res.requests == 4
    stocks = _sheet(res, "stocks")
    assert {"P/E", "EV/EBITDA", "Долг/EBITDA"} <= set(stocks.duplicates)
    # №, Действие + 6 формульных колонок §3.2; P/E и др. с occurrence: 1 — без ошибки неоднозначности
    assert stocks.writers == {"apps_script": 12, "formula": 8, "owner": 3}
    assert not [w for w in stocks.warnings if "writers" in w]
    assert _sheet(res, "video_tags").writers == {"n8n": 6}
    assert _sheet(res, "journal").last_data_row == 7  # данные с 5-й, 3 строки
    assert _sheet(res, "model").status == "no_header"


def test_missing_sheet():
    res, _ = _run(make_books(CONTRACT, drop_sheets=["Облигации"]))
    assert res.exit_code == EXIT_MISSING
    assert _sheet(res, "bonds").status == "missing"


def test_missing_optional_sheet_is_warning_only(ctx):
    from app.user_settings import load_rules

    rules = load_rules(REPO_ROOT / "config" / "settings_defaults.yaml")
    res, _ = _run(make_books(CONTRACT, drop_sheets=["Docker: настройки"]), rules=rules)
    assert res.exit_code == EXIT_OK
    assert _sheet(res, "docker_settings").status == "missing_optional"
    assert res.settings_source == "defaults" and res.settings_warnings
    assert res.settings_values["write.dry_run"] is True


def test_missing_header():
    res, _ = _run(make_books(CONTRACT, drop_headers={"Журнал": ["НКД"]}))
    assert res.exit_code == EXIT_MISSING
    col = next(c for c in _sheet(res, "journal").columns if c.name == "НКД")
    assert col.status == "missing"


def test_ambiguous_duplicate_header():
    res, _ = _run(make_books(CONTRACT, extra_headers={"Журнал": ["Дата"]}))
    assert res.exit_code == EXIT_MISSING
    j = _sheet(res, "journal")
    assert next(c for c in j.columns if c.name == "Дата").status == "ambiguous"
    assert "Дата" in j.duplicates


def test_size_warnings():
    books = make_books(CONTRACT)
    res, _ = _run(books, grids={"Журнал": (1000, 40)})
    assert res.exit_code == EXIT_OK
    w = " ".join(_sheet(res, "journal").warnings)
    assert "пустой хвост" in w and "колонок 40" in w


def test_access_error():
    res, _ = _run(make_books(CONTRACT), fail="list")
    assert res.exit_code == EXIT_ACCESS
    res, _ = _run(make_books(CONTRACT), fail="batch")
    assert res.exit_code == EXIT_ACCESS


def test_settings_read_from_sheet():
    from app.user_settings import load_rules

    rules = load_rules(REPO_ROOT / "config" / "settings_defaults.yaml")
    rows = [["write.dry_run", "нет", "", ""], ["tinvest.period_months", "2", "", ""]]
    res, _ = _run(make_books(CONTRACT, settings_rows=rows), rules=rules)
    assert res.settings_source == "sheet"
    assert res.settings_values["write.dry_run"] is False and res.settings_values["tinvest.period_months"] == 2
    assert res.requests == 4


def test_job_writes_report_and_exit_codes(ctx):
    ctx.reader_factory = lambda s: FakeSheetsReader(make_books(CONTRACT, drop_headers={"Идеи": ["Цель"]}))
    out = io.StringIO()
    result = run_job(ctx, "check", check.job, "manual", out=out)
    assert result.exit_code == EXIT_MISSING and result.status == "warn"
    reports = list(ctx.settings.reports_dir.glob("check_*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "❌ Цель" in text and "fake-invest" not in text  # ID таблиц не попадают в отчёт
    assert "docker-cleanup.ps1" in text  # первое напоминание
    runs = ctx.db.recent_runs()
    assert runs[0].job == "check" and runs[0].status == "warn"

    # второе выполнение в тот же день — без напоминания; JSON-вывод
    ctx.reader_factory = lambda s: FakeSheetsReader(make_books(CONTRACT))
    out = io.StringIO()
    result = run_job(ctx, "check", check.job, "manual", as_json=True, out=out)
    data = json.loads(out.getvalue())
    assert result.exit_code == EXIT_OK and data["exit_code"] == 0
    assert not any("docker-cleanup" in w for w in data["warnings"])


def test_job_no_secret_file(ctx):
    (ctx.settings.secrets_dir / "google_sa").unlink()
    from app.context import default_reader_factory

    ctx.reader_factory = default_reader_factory
    out = io.StringIO()
    result = run_job(ctx, "check", check.job, "manual", out=out)
    assert result.exit_code == EXIT_ACCESS and result.status == "error"
    assert "файл секрета не найден: google_sa" in out.getvalue()


def test_job_no_env(ctx):
    ctx.settings.env.pop("SHEET_BUDGET_ID")
    out = io.StringIO()
    result = run_job(ctx, "check", check.job, "manual", out=out)
    assert result.exit_code == EXIT_ACCESS
    assert "SHEET_BUDGET_ID" in out.getvalue()


def test_job_saves_user_settings_for_scheduler(ctx):
    rows = [["schedule.tinvest_import", "22:10", "", ""]]
    ctx.reader_factory = lambda s: FakeSheetsReader(make_books(CONTRACT, settings_rows=rows))
    run_job(ctx, "check", check.job, "manual", out=io.StringIO())
    saved = json.loads(ctx.db.get_meta("user_settings.values"))
    assert saved["schedule.tinvest_import"] == "22:10"
