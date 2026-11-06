import json
import logging

import pytest

from app.logging_setup import (
    JsonFormatter,
    SecretMaskingFilter,
    log_context,
    looks_like_secret,
    mask_secrets,
    setup_logging,
)
from app.settings import Settings, SettingsError

TOKEN = "t." + "Ab1_-" * 12
PEM = (
    "-----BEGIN PRIVATE KEY-----\n"  # pragma: allowlist secret
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
)


@pytest.mark.parametrize("secret", [
    TOKEN,
    PEM,
    '"private_key": "-----BEGIN abc\\nzzz"',
    "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9w",  # pragma: allowlist secret
    "token=supersecretvalue123",
])
def test_mask(secret):
    text = f"ошибка: {secret} конец"
    masked = mask_secrets(text)
    assert "***" in masked and masked.startswith("ошибка:") and masked.endswith("конец")
    for frag in ("Ab1_-Ab1_-", "MIIEvQIB", "zzz", "QUJDREVG", "supersecretvalue"):
        assert frag not in masked
    assert looks_like_secret(secret)


def test_plain_text_untouched():
    for text in ("Журнал: не найден заголовок «НКД»", "21:30", "да", "SBER", "RU000A0JX0J2"):
        assert mask_secrets(text) == text
        assert not looks_like_secret(text)


def test_log_masks_message_args_and_exception(tmp_path, capsys):
    setup_logging(logs_dir=tmp_path / "logs")
    log = logging.getLogger("test")
    with log_context("check", "abc123"):
        log.warning("токен %s", TOKEN)
        try:
            raise ValueError(f"плохой ключ {PEM}")
        except ValueError:
            log.exception("сбой")
    for h in logging.getLogger().handlers:
        h.flush()
    err = capsys.readouterr().err
    file_text = (tmp_path / "logs" / "finmodel.log").read_text(encoding="utf-8")
    for out in (err, file_text):
        assert TOKEN not in out and "MIIEvQIB" not in out
        assert "· WARNING · check · abc123 · токен ***" in out
        assert "ValueError" in out
    setup_logging()  # вернуть консольный обработчик без файла


def test_json_formatter_and_bad_args():
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "a %s %s", ("only-one",), None)
    SecretMaskingFilter().filter(rec)  # кривые аргументы не роняют логирование
    data = json.loads(JsonFormatter("Europe/Moscow").format(rec))
    assert data["level"] == "INFO" and "a %s %s" in data["message"]


def test_logs_dir_unwritable(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    setup_logging(logs_dir=blocker / "logs", console_format="json")
    assert "файловый лог недоступен" in capsys.readouterr().err
    setup_logging()


def test_settings_missing_secret_clear_error(tmp_path):
    s = Settings(secrets_dir=tmp_path)
    with pytest.raises(SettingsError) as e:
        s.read_secret("google_sa")
    assert str(e.value) == "файл секрета не найден: google_sa"


def test_settings_secret_never_in_error(tmp_path):
    (tmp_path / "empty").write_text("")
    (tmp_path / "bin").write_bytes(b"\xff\xfe" + TOKEN.encode())
    s = Settings(secrets_dir=tmp_path)
    with pytest.raises(SettingsError, match="пуст"):
        s.secret_path("empty")
    with pytest.raises(SettingsError) as e:
        s.read_secret("bin")
    assert TOKEN not in str(e.value) and e.value.__cause__ is None
    (tmp_path / "ok").write_text(TOKEN + "\n")
    assert s.read_secret("ok") == TOKEN
    assert TOKEN not in repr(s)


def test_settings_from_env():
    s = Settings.from_env({"FINMODEL_DATA_DIR": "/x", "TZ": "", "SHEET_INVEST_ID": "id1"})
    assert str(s.db_path) == "/x/finmodel.db" and s.tz == "Europe/Moscow"
    assert s.require_env("SHEET_INVEST_ID") == "id1"
    with pytest.raises(SettingsError, match="SHEET_BUDGET_ID"):
        s.require_env("SHEET_BUDGET_ID")
    with pytest.raises(SettingsError):
        Settings.from_env({"FINMODEL_LOG_FORMAT": "xml"})
    assert "id1" not in repr(s)
