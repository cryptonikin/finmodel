"""Общий контекст запуска: настройки, БД, фабрика читателя таблиц, конфиги."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import yaml

from app.db import Database, utcnow
from app.settings import GOOGLE_SA_SECRET, Settings
from app.sheets.client import GoogleSheetsReader, SheetsReader
from app.sheets.contract import Contract, load_contract
from app.user_settings import SettingRule, load_rules


class ConfigError(Exception):
    pass


def default_reader_factory(settings: Settings) -> SheetsReader:
    return GoogleSheetsReader(settings.secret_path(GOOGLE_SA_SECRET))


def load_yaml(path: Any) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise ConfigError(f"не читается {path.name}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: ожидается словарь")
    return data


@dataclass
class AppContext:
    settings: Settings
    db: Database
    reader_factory: Callable[[Settings], SheetsReader] = default_reader_factory
    now: Callable[[], datetime] = utcnow
    _cache: dict[str, Any] = field(default_factory=dict)

    def contract(self) -> Contract:
        if "contract" not in self._cache:
            self._cache["contract"] = load_contract(self.settings.config_dir / "sheets_map.yaml")
        return self._cache["contract"]

    def retention(self) -> dict[str, Any]:
        if "retention" not in self._cache:
            self._cache["retention"] = load_yaml(self.settings.config_dir / "retention.yaml")
        return self._cache["retention"]

    def rules(self) -> dict[str, SettingRule]:
        if "rules" not in self._cache:
            self._cache["rules"] = load_rules(self.settings.config_dir / "settings_defaults.yaml")
        return self._cache["rules"]
