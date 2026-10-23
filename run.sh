#!/usr/bin/env sh
# Запуск на Linux/macOS: ./run.sh  (проверка настроек: ./run.sh check)
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
[ -f config.yaml ] || { cp config.example.yaml config.yaml; echo "Создан config.yaml — заполни его и запусти снова."; exit 1; }
[ -f .env ] || cp .env.example .env
exec .venv/bin/python -m platesh "$@"
