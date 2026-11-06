# finmodel

Python-сервис финмодели в Docker (сначала ноутбук владельца, потом VPS). Работает **параллельно** с Apps Script
таблицы «Расчет инвестиций». Источник правды — Google Таблицы.

| Этап | Что | Статус |
|---|---|---|
| Ф0 | каркас + `check`: проверка таблиц **только на чтение**, планировщик с догоном, очистка, защита секретов | **готово (0.1.0)** |
| Ф1 | импорт операций Т-Банка → «Журнал» (`docs/tasks/TASK_F1_v1.2.md`) | после приёмки Ф0 |
| Ф2 | перенос расчётов MOEX/ЦБ, лист «Сверка» | — |
| Ф3 | Telegram — только уведомления | — |
| Ф4 | перенос на VPS | — |

Документы (версия — в имени файла; новая версия = новый файл, старый удаляется):

| Файл | Что |
|---|---|
| `docs/ARCHITECTURE_v1.1.md` | схема проекта: компоненты, доступы, зависимости этапов |
| `docs/SHEETS_INTERFACE_v1.8.md` | **главный контракт таблицы**: листы, колонки, кто что пишет (при расхождении с ТЗ верен он) |
| `docs/DECISIONS_v1.7.md` | журнал решений |
| `docs/tasks/TASK_F0_v1.4.md`, `docs/tasks/TASK_F1_v1.2.md` | ТЗ этапов |
| `config/sheets_map.yaml` | машиночитаемый контракт листов, сверен с SHEETS_INTERFACE v1.8 |

## Где что лежит (Windows)

```
D:\finmodel\code\                  ← этот репозиторий (github.com/cryptonikin/finmodel, приватный)
D:\finmodel\secrets\finmodel.env   ← SHEET_INVEST_ID=…, SHEET_BUDGET_ID=…, TZ=Europe/Moscow (шаблон: .env.example)
D:\finmodel\secrets\google-sa.json ← ключ сервисного аккаунта Google (Ф0: доступ только на чтение)
D:\finmodel\data\                  ← SQLite (finmodel.db), отчёты (reports\), логи (logs\), бэкапы (backups\)
```

Секреты **никогда** не кладутся в репозиторий. Права на папку `secrets` — только у владельца.
Сервисному аккаунту открыть доступ «Читатель» к двум таблицам: «Расчет инвестиций», «Бюджет и инвестиции - ai».

## Запуск (Windows PowerShell, из `D:\finmodel\code`)

```powershell
docker compose build
docker compose run --rm app check            # проверка таблиц (только чтение)
docker compose run --rm app check --json     # то же, вывод JSON
docker compose run --rm app runs --last 20   # журнал запусков
docker compose run --rm app cleanup --dry-run   # что удалила бы очистка
docker compose run --rm app version

# фоновый планировщик (включать после приёмки):
docker compose --profile scheduler up -d
docker compose logs -f scheduler
docker compose --profile scheduler down
```

Коды выхода `check`: **0** — всё найдено; **1** — нет листа/колонки или заголовок неоднозначен;
**2** — нет доступа, секретов или переменных окружения; **3** — занято другой задачей.
Отчёт: `D:\finmodel\data\reports\check_ГГГГММДД_ЧЧММ.md`.

Разовые команды — только `docker compose run --rm …` (контейнер удаляется после выполнения).

**Если работает планировщик** (`scheduler` запущен), разовые команды выполняйте **внутри него**, а не через `run`:

```powershell
docker compose exec scheduler python -m app check
docker compose exec scheduler python -m app runs --last 20
docker compose exec scheduler python -m app cleanup --dry-run
```

Так команда и планировщик используют одну файловую блокировку `/data/finmodel.lock` в одном контейнере, и задачи
не выполняются одновременно. Блокировка между разными контейнерами на томе Windows (Docker Desktop) не гарантирована.
Если задача уже идёт, команда завершится с кодом 3 («занято»), повторите позже.

## Расписание и догон

`config/schedule.yaml` (время МСК): `check` 12:30 (окно между запусками Apps Script в 12 и 13 ч), `cleanup` 21:45. Если ноутбук был выключен и слот пропущен,
планировщик при старте выполнит задачу **один раз** (сколько бы слотов ни было пропущено).
Последние запуски — `runs`, журнал хранится 90 дней.

## Настройки — где что

| Где | Что | Кто меняет |
|---|---|---|
| Лист «Docker: настройки» (4 строки) | `enabled.tinvest_import`, `tinvest.period_months` (1–3), `write.dry_run`, `schedule.tinvest_import` | владелец |
| `D:\finmodel\secrets\finmodel.env` | ID таблиц, фильтр счетов Т-Банка, `TZ` | владелец, редко |
| Docker secrets (`D:\finmodel\secrets\…`) | ключ Google, (Ф1) токен Т-Банка | владелец, при ротации |
| `config/*.yaml` в репо | расписание, сроки хранения, лимиты, контракт листов | через Claude Code + git |

Нет листа или неверное значение → значение из `config/settings_defaults.yaml` + предупреждение в `check`.
Docker лист настроек **только читает**. Секретов в листе быть не может (такие значения игнорируются).

## Хранение данных и очистка

Задача `cleanup` (21:45 + догон) держит `/data` маленьким: логи 30 дней, журнал запусков 90 дней, отчёты 30 дней
(не больше 100), бэкапы — последние 10 не старше 30 дней, но последние 3 хранятся всегда; сроки — `config/retention.yaml`. Ничего вне `/data` не
удаляется. Логи контейнеров ограничены 10 МБ × 3, `/tmp` в контейнере — 64 МБ, корень — только чтение.

Раз в месяц (напоминание приходит в отчёте `check`) — очистка Docker на хосте:
```powershell
.\scripts\docker-cleanup.ps1     # только «висячие» образы и кэш сборки; без -a и --volumes
```

## Безопасность

- В Ф0 сервис **ничего не пишет** в таблицы; scope Google — только `spreadsheets.readonly`.
- Логи и отчёты маскируют всё, похожее на токен/ключ; значения секретов не выводятся даже в ошибках.
- Перед `git push`: `.\scripts\check-secrets.ps1` (gitleaks в Docker). В контейнере Claude Code — pre-commit
  hook detect-secrets (`scripts/install-hooks.sh`).
- Контейнер: не root (uid 10001), `read_only`, `cap_drop: ALL`, `no-new-privileges`, без портов, лимит 512 МБ.

## Разработка

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest --cov=app        # тесты без сети, только подделки
.venv/bin/ruff check .
bash scripts/install-hooks.sh
```
