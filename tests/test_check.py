import io
import json

import pytest

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
    return run_check(CONTRACT, IDS, reader, rules=kw.get("rules"), previous_sizes=kw.get("previous_sizes")), reader


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
    res, _ = _run(books, grids={"Журнал": (3000, 40)})
    assert res.exit_code == EXIT_OK
    w = " ".join(_sheet(res, "journal").warnings)
    assert "пустой хвост 2993 строк (> 2000)" in w and "колонок 40" in w
    # хвост 993 < 2000 — без предупреждения (порог поднят с 300)
    res, _ = _run(books, grids={"Журнал": (1000, 20)})
    assert not _sheet(res, "journal").warnings


def test_size_limits_from_config():
    from app.context import ConfigError, load_yaml

    cfg = load_yaml(REPO_ROOT / "config" / "retention.yaml")
    assert check.SizeLimits.from_config(cfg.get("sheet_size")) == check.SizeLimits(2000, 5)
    assert check.SizeLimits.from_config(None) == check.SizeLimits()
    assert check.SizeLimits.from_config({"empty_tail_rows": 50}).empty_tail_rows == 50
    with pytest.raises(ConfigError):
        check.SizeLimits.from_config({"empty_tail_rows": "много"})


def _growth(res):
    return [w for s in res.sheets for w in s.warnings if "вырос" in w]


def test_growth_first_run_no_warning():
    res, _ = _run(make_books(CONTRACT), grids={"Журнал": (500, 20)})
    assert res.exit_code == EXIT_OK and not _growth(res)
    res2, _ = _run(make_books(CONTRACT), grids={"Журнал": (500, 20)}, previous_sizes={})
    assert not _growth(res2)
    assert check.sheet_sizes(res)["journal"] == [500, 20]


def test_growth_rows_and_columns_warn():
    res, _ = _run(make_books(CONTRACT), grids={"Журнал": (520, 22)}, previous_sizes={"journal": [500, 20]})
    assert res.exit_code == EXIT_OK  # рост — предупреждение, не ошибка
    w = _growth(res)
    assert len(w) == 1 and "строк 500 → 520 (+20)" in w[0] and "колонок 20 → 22 (+2)" in w[0]
    res, _ = _run(make_books(CONTRACT), grids={"Журнал": (500, 21)}, previous_sizes={"journal": [500, 20]})
    assert _growth(res) and "строк" not in _growth(res)[0]


def test_no_growth_no_warning():
    prev = {"journal": [500, 20]}
    res, _ = _run(make_books(CONTRACT), grids={"Журнал": (500, 20)}, previous_sizes=prev)
    assert not _growth(res)
    res, _ = _run(make_books(CONTRACT), grids={"Журнал": (400, 18)}, previous_sizes=prev)  # уменьшение — молча
    assert not _growth(res)


def test_video_tags_type_columns_not_ambiguous():
    real = ["Тип (факт_макро / мнение / позиция_автора / …)", "Тип данных (= мнение аналитика)"]
    books = make_books(CONTRACT, drop_headers={"Теги_видео": ["Тип (факт_макро", "Тип данных"]},
                       extra_headers={"Теги_видео": real})
    res, _ = _run(books)
    tags = _sheet(res, "video_tags")
    assert res.exit_code == EXIT_OK, check.render_console(res)
    assert {c.name: c.match for c in tags.columns}["Тип (факт_макро"] == "prefix"
    assert all(c.status == "found" for c in tags.columns)


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


def test_job_growth_baseline_in_meta(ctx):
    sizes = {"Журнал": (500, 20)}
    ctx.reader_factory = lambda s: FakeSheetsReader(make_books(CONTRACT), grids=sizes)

    # первый запуск: базы нет — предупреждения о росте нет, база сохранена
    run_job(ctx, "check", check.job, "manual", out=io.StringIO())
    base = json.loads(ctx.db.get_meta(check.META_SHEET_SIZES))
    assert base["journal"] == [500, 20] and "model" in base
    assert not any("вырос" in r.read_text(encoding="utf-8") for r in ctx.settings.reports_dir.glob("check_*.md"))

    # без роста — молча
    out = io.StringIO()
    run_job(ctx, "check", check.job, "manual", out=out)
    assert "вырос" not in out.getvalue()

    # нет доступа — база не меняется
    ctx.reader_factory = lambda s: FakeSheetsReader(make_books(CONTRACT), grids={"Журнал": (900, 20)}, fail="list")
    run_job(ctx, "check", check.job, "manual", out=io.StringIO())
    assert json.loads(ctx.db.get_meta(check.META_SHEET_SIZES))["journal"] == [500, 20]

    # рост — предупреждение, затем новая база
    sizes["Журнал"] = (600, 20)
    ctx.reader_factory = lambda s: FakeSheetsReader(make_books(CONTRACT), grids=sizes)
    out = io.StringIO()
    result = run_job(ctx, "check", check.job, "manual", out=out)
    assert "лист вырос с прошлой проверки: строк 500 → 600 (+100)" in out.getvalue()
    assert result.status == "warn" and result.exit_code == EXIT_OK
    assert json.loads(ctx.db.get_meta(check.META_SHEET_SIZES))["journal"] == [600, 20]
    out = io.StringIO()
    run_job(ctx, "check", check.job, "manual", out=out)
    assert "вырос" not in out.getvalue()
