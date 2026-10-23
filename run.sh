#!/usr/bin/env sh
# Запуск на Linux/macOS: ./run.sh — при первом запуске задаст вопросы и всё настроит сам.
#   ./run.sh setup  — настроить заново
#   ./run.sh check  — проверить настройки
set -e
cd "$(dirname "$0")"
if [ ! -f .venv/.installed ]; then
  echo "Устанавливаю зависимости (один раз)..."
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
  touch .venv/.installed
fi
exec .venv/bin/python -m platesh "$@"
