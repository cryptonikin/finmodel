# Ежемесячная очистка Docker на хосте (TASK_F0 §13.2). Запуск: .\scripts\docker-cleanup.ps1
# Удаляет ТОЛЬКО «висячие» образы (после пересборки) и кэш сборки старше 30 дней.
# НЕ использовать -a и --volumes: удалятся образ n8n и данные.
$ErrorActionPreference = "Stop"
Write-Host "До очистки:"; docker system df
docker image prune -f
docker builder prune -f --filter "until=720h"
Write-Host "После очистки:"; docker system df
