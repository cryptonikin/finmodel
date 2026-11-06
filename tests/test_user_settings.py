from pathlib import Path

import pytest

from app.settings import REPO_ROOT
from app.sheets.client import SheetsAccessError
from app.user_settings import (
    SettingsRulesError,
    defaults,
    load_rules,
    load_user_settings,
    parse_rows,
)
from tests.conftest import FakeSheetsReader

RULES = load_rules(REPO_ROOT / "config" / "settings_defaults.yaml")
HEAD = ["Параметр", "Значение", "Описание", "Допустимые значения"]


def test_defaults():
    d = defaults(RULES)
    assert d.values == {"enabled.tinvest_import": False, "tinvest.period_months": 3, "write.dry_run": True,
                        "schedule.tinvest_import": "21:30"}
    assert d.source == "defaults"


def test_normal_sheet():
    rows = [HEAD, ["enabled.tinvest_import", "ДА"], ["tinvest.period_months", "2,0"],
            ["write.dry_run", " Нет "], ["schedule.tinvest_import", "9:05"], ["", ""]]
    s = parse_rows(rows, RULES)
    assert s.source == "sheet" and not s.warnings
    assert s.values == {"enabled.tinvest_import": True, "tinvest.period_months": 2, "write.dry_run": False,
                        "schedule.tinvest_import": "09:05"}


@pytest.mark.parametrize("key, raw, expected", [
    ("tinvest.period_months", "5", 3),
    ("tinvest.period_months", "2,5", 3),
    ("tinvest.period_months", "abc", 3),
    ("write.dry_run", "может быть", True),
    ("schedule.tinvest_import", "24:00", "21:30"),
    ("enabled.tinvest_import", "", False),
])
def test_invalid_value_falls_back(key, raw, expected):
    s = parse_rows([HEAD, [key, raw]], RULES)
    assert s.values[key] == expected
    assert len(s.warnings) == 1 and key in s.warnings[0]


def test_unknown_key_and_duplicate():
    s = parse_rows([HEAD, ["foo.bar", "1"], ["write.dry_run", "нет"], ["write.dry_run", "да"], ["short"]], RULES)
    assert s.values["write.dry_run"] is False
    assert any("foo.bar" in w for w in s.warnings) and any("повторяется" in w for w in s.warnings)


def test_secret_in_value_ignored_and_not_echoed():
    token = "t." + "x" * 60
    s = parse_rows([HEAD, ["write.dry_run", token], [token, "да"]], RULES)
    assert s.values["write.dry_run"] is True
    assert len(s.warnings) == 2
    assert all(token not in w and "секрет" in w for w in s.warnings)


def test_no_sheet_and_bad_headers():
    reader = FakeSheetsReader({"id": {"Другой": [["x"]]}})
    s = load_user_settings(reader, "id", RULES)
    assert s.source == "defaults" and "не найден" in s.warnings[0]
    assert parse_rows([], RULES).warnings
    assert "не найден" in parse_rows([["Ключ", "Знач"]], RULES).warnings[0]


def test_load_from_sheet_and_access_error():
    reader = FakeSheetsReader({"id": {"Docker: настройки": [HEAD, ["tinvest.period_months", "1"]]}})
    assert load_user_settings(reader, "id", RULES).values["tinvest.period_months"] == 1
    bad = FakeSheetsReader({"id": {}}, fail="list")
    s = load_user_settings(bad, "id", RULES)
    assert s.source == "defaults" and "не прочитаны" in s.warnings[0]
    assert isinstance(SheetsAccessError("x"), Exception)


@pytest.mark.parametrize("text", [
    "x: 1\n",
    "x: {type: float, default: 1}\n",
    "x: {type: bool, default: 'да'}\n",
    "x: {type: int, default: 9, min: 1, max: 3}\n",
    "x: [\n",
])
def test_bad_rules(tmp_path: Path, text):
    p = tmp_path / "r.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(SettingsRulesError):
        load_rules(p)
