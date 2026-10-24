#!/usr/bin/env bash
# Установка Platesh на VPS-сервер (Ubuntu 22.04+ / Debian 12+) одной командой:
#
#   curl -fsSL https://raw.githubusercontent.com/gytfel/Platesh/refs/heads/claude/beautiful-johnson-i6jquz/install.sh | bash
#
# (если зашёл не под root — добавь sudo:  ... | sudo bash)
# Повторный запуск той же команды обновляет Platesh (настройки и база сохраняются).
set -euo pipefail

REPO="${PLATESH_REPO:-https://github.com/gytfel/Platesh.git}"
BRANCH="${PLATESH_BRANCH:-claude/beautiful-johnson-i6jquz}"
DIR="${PLATESH_DIR:-/opt/platesh}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "запусти с правами администратора: ... | sudo bash"
command -v apt-get >/dev/null || fail "установщик рассчитан на Ubuntu или Debian"

say "Устанавливаю системные пакеты (git, Python)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || fail "нужен Python 3.10+, а здесь $(python3 --version). Возьми сервер с Ubuntu 22.04/24.04 или Debian 12"

if [ -d "$DIR/.git" ]; then
  say "Обновляю Platesh в $DIR..."
  git -C "$DIR" pull -q --ff-only origin "$BRANCH"
elif [ -d "$DIR" ] && [ -n "$(ls -A "$DIR")" ]; then
  # Папка уже есть — например, с перенесёнными с другого сервера config.yaml, .env и data/.
  # Скачиваем код прямо в неё: файлы программы появятся, настройки и база останутся.
  say "Скачиваю Platesh в $DIR (найденные там настройки и база сохранятся)..."
  git -C "$DIR" init -q
  git -C "$DIR" remote add origin "$REPO" 2>/dev/null || git -C "$DIR" remote set-url origin "$REPO"
  git -C "$DIR" fetch -q --depth 1 origin "$BRANCH"
  git -C "$DIR" checkout -q -f -B "$BRANCH" FETCH_HEAD
else
  say "Скачиваю Platesh в $DIR..."
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi
# Чтобы обычный «git pull» в этой папке знал, откуда обновляться (при переносе код скачан без этой связи).
git -C "$DIR" branch -q --set-upstream-to="origin/$BRANCH" "$BRANCH" 2>/dev/null || true
cd "$DIR"
chmod +x run.sh install.sh

say "Устанавливаю зависимости Python..."
./run.sh genkey >/dev/null

if [ "${PLATESH_SKIP_SETUP:-}" != "1" ]; then
  [ -r /dev/tty ] || fail "нет терминала для вопросов. Подключись к серверу по SSH и запусти команду установки там"
  say "Настройка — ответь на вопросы"
  ./run.sh setup --no-start </dev/tty
fi
[ -f config.yaml ] || fail "настройка не завершена. Запусти команду установки ещё раз"

if [ "${PLATESH_SKIP_SERVICE:-}" = "1" ]; then
  say "Готово (служба не устанавливалась). Запуск вручную: cd $DIR && ./run.sh"
  exit 0
fi
say "Включаю постоянную работу (служба systemd)..."
./run.sh service

say "Готово! Platesh работает на сервере."
cat <<INFO
   Журнал (что происходит):     journalctl -u platesh -f      (выход — Ctrl+C)
   Перенастроить:               cd $DIR && ./run.sh setup --no-start && systemctl restart platesh
   Обновить:                    та же команда установки ещё раз

   ⚠️ Если Platesh был запущен на компьютере — останови его там:
      один бот не может работать в двух местах сразу.
INFO
