"""Технические настройки: env + пути к секретам.

Значения секретов никогда не печатаются и не попадают в текст ошибок — только имя секрета.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GOOGLE_SA_SECRET = "google_sa"  # noqa: S105 — имя файла секрета, не значение  # pragma: allowlist secret


class SettingsError(Exception):
    """Ошибка конфигурации (нет переменной, нет файла секрета). Текст без значений."""


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("/data")
    secrets_dir: Path = Path("/run/secrets")
    config_dir: Path = REPO_ROOT / "config"
    tz: str = "Europe/Moscow"
    log_format: str = "text"  # text | json — формат консоли
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        log_format = env.get("FINMODEL_LOG_FORMAT", "text").strip().lower()
        if log_format not in ("text", "json"):
            raise SettingsError("FINMODEL_LOG_FORMAT: допустимо text или json")
        return cls(
            data_dir=Path(env.get("FINMODEL_DATA_DIR", "/data")),
            secrets_dir=Path(env.get("FINMODEL_SECRETS_DIR", "/run/secrets")),
            config_dir=Path(env.get("FINMODEL_CONFIG_DIR", str(REPO_ROOT / "config"))),
            tz=env.get("TZ", "Europe/Moscow") or "Europe/Moscow",
            log_format=log_format,
            env=env,
        )

    # --- каталоги в /data ---
    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "finmodel.db"

    # --- env ---
    def require_env(self, name: str) -> str:
        value = (self.env.get(name) or "").strip()
        if not value:
            raise SettingsError(f"не задана переменная окружения: {name}")
        return value

    # --- секреты ---
    def secret_path(self, name: str) -> Path:
        path = self.secrets_dir / name
        if not path.is_file():
            raise SettingsError(f"файл секрета не найден: {name}")
        try:
            size = path.stat().st_size
        except OSError:  # pragma: no cover — права/ФС
            raise SettingsError(f"файл секрета недоступен: {name}") from None
        if size == 0:
            raise SettingsError(f"файл секрета пуст: {name}")
        return path

    def read_secret(self, name: str) -> str:
        path = self.secret_path(name)
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            # исходное исключение не прикрепляем: в нём может оказаться часть содержимого
            raise SettingsError(f"файл секрета не читается: {name}") from None
