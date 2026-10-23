#!/usr/bin/env sh
# Запуск на Linux/macOS: ./run.sh — при первом запуске задаст вопросы и всё настроит сам.
#   ./run.sh setup   — настроить заново
#   ./run.sh check   — проверить настройки
#   ./run.sh update  — скачать новую версию (настройки сохранятся)
#   sudo ./run.sh service — на сервере: работать постоянно (служба systemd)
set -e
cd "$(dirname "$0")"
# Зависимости ставим заново, если изменился requirements.txt (копия лежит в .venv/.installed).
if ! cmp -s requirements.txt .venv/.installed 2>/dev/null; then
  echo "Устанавливаю зависимости..."
  if [ ! -d .venv ] && ! python3 -m venv .venv; then
    echo "Не получилось создать окружение Python. На Ubuntu/Debian выполни: apt install -y python3-venv"
    exit 1
  fi
  .venv/bin/pip install -q -r requirements.txt
  cp requirements.txt .venv/.installed
fi
exec .venv/bin/python -m platesh "$@"
