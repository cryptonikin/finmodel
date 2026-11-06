#!/usr/bin/env bash
# Ставит git pre-commit hook: detect-secrets по индексируемым файлам (TASK_F0 §4).
# Если detect-secrets не установлен (например, на Windows у владельца) — хук предупреждает и НЕ блокирует.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
hook=".git/hooks/pre-commit"
cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
# finmodel: detect-secrets pre-commit (установлен scripts/install-hooks.sh)
root="$(git rev-parse --show-toplevel)"
bin=""
for c in "$root/.venv/bin/detect-secrets-hook" "$(command -v detect-secrets-hook 2>/dev/null || true)"; do
  if [ -n "$c" ] && [ -x "$c" ]; then bin="$c"; break; fi
done
if [ -z "$bin" ]; then
  echo "ВНИМАНИЕ: detect-secrets не установлен — проверка секретов пропущена." >&2
  echo "          Перед push запустите scripts\\check-secrets.ps1 (gitleaks в Docker)." >&2
  exit 0
fi
files=()
while IFS= read -r -d '' f; do files+=("$f"); done < <(git diff --cached --name-only --diff-filter=ACM -z)
[ ${#files[@]} -eq 0 ] && exit 0
if ! "$bin" --baseline "$root/.secrets.baseline" "${files[@]}"; then
  echo "ОСТАНОВЛЕНО: detect-secrets нашёл похожее на секрет. Уберите секрет из файла." >&2
  echo "Ложное срабатывание: detect-secrets scan --baseline .secrets.baseline и проверьте audit." >&2
  exit 1
fi
HOOK
chmod +x "$hook"
echo "pre-commit hook установлен: $hook"
