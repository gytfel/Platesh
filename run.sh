#!/usr/bin/env sh
# Запуск на Linux/macOS: ./run.sh  (проверка настроек: ./run.sh check)
set -e
cd "$(dirname "$0")"
if [ ! -f .venv/.installed ]; then
  echo "Устанавливаю зависимости (один раз)..."
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
  touch .venv/.installed
fi
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  [ -f .env ] || cp .env.example .env
  echo "Созданы config.yaml и .env — впиши в них токены и настройки, затем запусти снова."
  exit 1
fi
[ -f .env ] || cp .env.example .env
exec .venv/bin/python -m platesh "$@"
