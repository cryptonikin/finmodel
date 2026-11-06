# CHANGELOG

## 0.1.1 — 06.11.2026 — Ф0: доработки по приёмке

- `cleanup`: последние 3 бэкапа (`backups.keep_min`) хранятся всегда, даже старше 30 дней.
- Dockerfile: системная `tzdata`, `/etc/localtime` = Europe/Moscow (ротация логов в полночь МСК).
- `check`: контроль роста листов — предупреждение, если строк/колонок больше, чем в прошлом успешном `check`
  (база в SQLite `meta`); порог пустого хвоста 300 → 2000, пороги в `config/retention.yaml` → `sheet_size`.
- `sheets_map.yaml`: «Теги_видео» — «Тип (факт_макро» вместо неоднозначного «Тип».
- README: при работающем планировщике разовые команды выполняются через `docker compose exec scheduler python -m app …`.

## 0.1.0 — 06.11.2026 — Ф0: каркас (TASK_F0 v1.4)

- CLI `python -m app {check|scheduler|runs|cleanup|version}`.
- `check`: проверка листов и заголовков обеих таблиц **только на чтение** (≤ 4 запросов к API), дубли заголовков,
  писатели колонок, контроль размера листов, настройки владельца из листа «Docker: настройки», отчёт
  `/data/reports/check_*.md`, коды выхода 0/1/2.
- Поиск колонки по заголовку: нормализация, точное → по началу, неоднозначность — ошибка, `occurrence` для дублей.
- Контракт листов `config/sheets_map.yaml` (+ `writers`, `optional`).
- Планировщик с догоном пропущенного слота (один раз), блокировка, остановка по SIGTERM; журнал `job_runs` в SQLite.
- `cleanup` по `config/retention.yaml`, только внутри `/data`, `--dry-run`, VACUUM.
- Логи: консоль + файл с ротацией по суткам (30 дней), маскировка секретов.
- Docker: не root, read-only, cap_drop ALL, без портов, лимиты логов и tmpfs; secrets.
- Защита секретов: `.gitignore`/`.dockerignore`, detect-secrets (baseline + pre-commit hook), gitleaks-скрипт.
