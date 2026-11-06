# Проверка репозитория на секреты перед git push (для владельца, Windows PowerShell).
# Нужен Docker Desktop. Запуск из D:\finmodel\code:  .\scripts\check-secrets.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
Write-Host "gitleaks: проверка истории git и файлов..."
docker run --rm -v "${PWD}:/repo" zricethezav/gitleaks:v8.30.1 detect --source=/repo --no-banner
if ($LASTEXITCODE -ne 0) {
    Write-Host "НАЙДЕНЫ возможные секреты — НЕ делайте push. Покажите вывод архитектору." -ForegroundColor Red
    exit 1
}
Write-Host "Секретов не найдено — можно делать git push." -ForegroundColor Green
