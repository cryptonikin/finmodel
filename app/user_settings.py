"""Настройки владельца: лист «Docker: настройки» → проверка → значения (TASK_F0 §10.1, §14).

Docker лист только читает. Нет листа, нет строки, неверное или неизвестное значение → значение по умолчанию
из config/settings_defaults.yaml + предупреждение (никогда не падаем). Значение, похожее на секрет, игнорируется,
а в предупреждении не печатается.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

import yaml

from app.logging_setup import looks_like_secret, mask_secrets
from app.sheets.client import SheetsAccessError, SheetsReader, a1_sheet
from app.sheets.headers import HeaderError, find_column, normalize

SETTINGS_ALIAS = "docker_settings"
SETTINGS_TITLE = "Docker: настройки"
PARAM_HEADER = "Параметр"
VALUE_HEADER = "Значение"
MAX_ROWS = 200
TYPES = ("bool", "int", "time")

_TRUE = {"да"}
_FALSE = {"нет"}
_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")


class SettingsRulesError(Exception):
    pass


@dataclass(frozen=True)
class SettingRule:
    key: str
    type: str
    default: Any
    min: int | None = None
    max: int | None = None
    description: str = ""


@dataclass
class UserSettings:
    values: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    source: str = "defaults"  # defaults | sheet

    def get(self, key: str) -> Any:
        return self.values[key]


def parse_value(rule: SettingRule, raw: object) -> Any:
    """Строка из листа → значение нужного типа. ValueError с понятным текстом (без самого значения)."""
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise ValueError("пусто")
    if rule.type == "bool":
        n = normalize(text)
        if n in _TRUE:
            return True
        if n in _FALSE:
            return False
        raise ValueError("ожидается да/нет")
    if rule.type == "int":
        num = text.replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            f = float(num)
        except ValueError:
            raise ValueError("ожидается число") from None
        if not f.is_integer():
            raise ValueError("ожидается целое число")
        v = int(f)
        if (rule.min is not None and v < rule.min) or (rule.max is not None and v > rule.max):
            raise ValueError(f"допустимо {rule.min}–{rule.max}")
        return v
    if rule.type == "time":
        m = _TIME.match(text)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError("ожидается ЧЧ:ММ")
        return time(int(m.group(1)), int(m.group(2))).strftime("%H:%M")
    raise ValueError(f"неизвестный тип {rule.type}")  # pragma: no cover — проверено в load_rules


def load_rules(path: Path) -> dict[str, SettingRule]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise SettingsRulesError(f"не читается {path.name}") from None
    if not isinstance(data, dict) or not data:
        raise SettingsRulesError(f"{path.name}: ожидается словарь ключей")
    rules: dict[str, SettingRule] = {}
    for key, spec in data.items():
        if not isinstance(spec, dict) or spec.get("type") not in TYPES or "default" not in spec:
            raise SettingsRulesError(f"{path.name}: {key}: нужны type ({'/'.join(TYPES)}) и default")
        rule = SettingRule(str(key), spec["type"], spec["default"], spec.get("min"), spec.get("max"),
                           str(spec.get("description", "")))
        if rule.type == "bool" and not isinstance(rule.default, bool):
            raise SettingsRulesError(f"{path.name}: {key}: default должен быть true/false")
        if rule.type != "bool":
            try:
                parse_value(rule, rule.default)
            except ValueError as exc:
                raise SettingsRulesError(f"{path.name}: {key}: default неверен ({exc})") from None
        rules[rule.key] = rule
    return rules


def defaults(rules: dict[str, SettingRule], warning: str | None = None) -> UserSettings:
    values = {k: (r.default if r.type == "bool" else parse_value(r, r.default)) for k, r in rules.items()}
    return UserSettings(values, [warning] if warning else [], "defaults")


def parse_rows(rows: Sequence[Sequence[object]], rules: dict[str, SettingRule]) -> UserSettings:
    """rows[0] — строка заголовков, далее данные."""
    result = defaults(rules)
    if not rows:
        result.warnings.append(f"лист «{SETTINGS_TITLE}» пуст — значения по умолчанию")
        return result
    try:
        pi = find_column(rows[0], PARAM_HEADER)
        vi = find_column(rows[0], VALUE_HEADER)
    except HeaderError as exc:
        result.warnings.append(f"лист «{SETTINGS_TITLE}»: {exc} — значения по умолчанию")
        return result
    result.source = "sheet"
    seen: set[str] = set()
    for n, row in enumerate(rows[1:], start=2):
        key = str(row[pi]).strip() if pi < len(row) else ""
        raw = row[vi] if vi < len(row) else ""
        if not key:
            continue
        if looks_like_secret(key) or looks_like_secret(str(raw)):
            result.warnings.append(f"строка {n}: значение похоже на секрет — проигнорировано "
                                   "(секретам не место в листе)")
            continue
        if key not in rules:
            result.warnings.append(f"строка {n}: неизвестный параметр «{mask_secrets(key)}» — пропущен")
            continue
        if key in seen:
            result.warnings.append(f"строка {n}: параметр {key} повторяется — взято первое значение")
            continue
        seen.add(key)
        try:
            result.values[key] = parse_value(rules[key], raw)
        except ValueError as exc:
            result.warnings.append(f"{key}: неверное значение ({exc}) — по умолчанию {rules[key].default}")
    return result


def load_user_settings(
    reader: SheetsReader, spreadsheet_id: str, rules: dict[str, SettingRule], title: str = SETTINGS_TITLE
) -> UserSettings:
    """Отдельное чтение листа настроек (2 запроса). check читает его в своём пакете запросов."""
    try:
        info = {s.title: s for s in reader.list_sheets(spreadsheet_id)}
        if title not in info:
            return defaults(rules, f"лист «{title}» не найден — значения по умолчанию")
        last = max(1, min(info[title].row_count, MAX_ROWS))
        rows = reader.batch_get(spreadsheet_id, [f"{a1_sheet(title)}!A1:Z{last}"])[0]
    except SheetsAccessError as exc:
        return defaults(rules, f"настройки не прочитаны ({exc}) — значения по умолчанию")
    return parse_rows(rows, rules)
