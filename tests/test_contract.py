from pathlib import Path

import pytest
import yaml

from app.settings import REPO_ROOT
from app.sheets.contract import ContractError, WriterSpec, load_contract, parse_contract


def base():
    return {
        "version": 1,
        "spreadsheets": {"invest": {"env": "SHEET_INVEST_ID", "title": "X"}},
        "sheets": [{"alias": "j", "spreadsheet": "invest", "title": "Журнал", "header_row": 4, "data_from": 5,
                    "key": "ID", "required": ["ID", {"name": "P/E", "occurrence": 1}],
                    "writers": {"ID": "owner"}}],
    }


def test_repo_contract_is_valid():
    c = load_contract(REPO_ROOT / "config" / "sheets_map.yaml")
    assert {s.alias for s in c.sheets} >= {"journal", "stocks", "bonds", "ideas", "reference", "candidates",
                                            "video_tags", "docker_settings", "model", "scoring", "budget_m"}
    assert c.sheet("docker_settings").optional
    stocks = c.sheet("stocks")
    assert any(r.name == "P/E" and r.occurrence == 1 for r in stocks.required)
    assert stocks.writers["№"].writer == "formula" and stocks.writers["Действие"].writer == "formula"
    assert stocks.writers["P/E"] == WriterSpec("apps_script", 1)
    assert c.sheet("journal").writers["ID"].writer == "formula"  # SHEETS_INTERFACE §3.3
    assert c.sheet("video_tags").writers["ID (hash)"].writer == "n8n"
    tags = {r.name for r in c.sheet("video_tags").required}
    assert {"Сценарий", "Статус проверки", "Модель LLM", "Версия промта"} <= tags  # схема v3 (§4)


def test_valid():
    c = parse_contract(base())
    s = c.sheet("j")
    assert s.required[1].label() == "P/E (#1)"
    assert c.sheets_of("invest") == [s]
    with pytest.raises(ContractError):
        c.sheet("nope")


def test_broken_yaml(tmp_path: Path):
    p = tmp_path / "m.yaml"
    p.write_text("version: [1\n", encoding="utf-8")
    with pytest.raises(ContractError, match="YAML"):
        load_contract(p)
    with pytest.raises(ContractError, match="не найден"):
        load_contract(tmp_path / "missing.yaml")


def test_unknown_spreadsheet_alias():
    d = base()
    d["sheets"][0]["spreadsheet"] = "other"
    with pytest.raises(ContractError, match="неизвестный алиас таблицы"):
        parse_contract(d)


def test_missing_env_names_variable_not_value():
    c = parse_contract(base())
    with pytest.raises(ContractError, match="SHEET_INVEST_ID"):
        c.resolve_ids({})
    assert c.resolve_ids({"SHEET_INVEST_ID": " abc "}) == {"invest": "abc"}


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda d: d.update(version=2), "version"),
        (lambda d: d.pop("spreadsheets"), "spreadsheets"),
        (lambda d: d.update(sheets=[]), "sheets"),
        (lambda d: d["sheets"][0].update(data_from=4), "data_from"),
        (lambda d: d["sheets"][0].pop("key"), "key"),
        (lambda d: d["sheets"][0].update(writers={"ID": "robot"}), "writers"),
        (lambda d: d["sheets"][0].update(writers={"ID": {"writer": "owner", "x": 1}}), "writers"),
        (lambda d: d["sheets"][0].update(writers={"ID": {"writer": "owner", "occurrence": 0}}), "writers"),
        (lambda d: d["sheets"][0].update(required=[{"name": "X", "occurrence": 0}]), "occurrence"),
        (lambda d: d["sheets"][0].update(required=[123]), "required"),
        (lambda d: d["sheets"][0].update(bogus=1), "неизвестные поля"),
        (lambda d: d["sheets"].append(dict(d["sheets"][0])), "повторяется"),
        (lambda d: d["sheets"].append({"alias": "m", "spreadsheet": "invest", "title": "M", "key": "x"}),
         "без header_row"),
        (lambda d: d["sheets"][0].update(optional="yes"), "optional"),
    ],
)
def test_invalid(mutate, msg):
    d = yaml.safe_load(yaml.safe_dump(base(), allow_unicode=True))
    mutate(d)
    with pytest.raises(ContractError, match=msg):
        parse_contract(d)
