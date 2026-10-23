@echo off
rem Запуск на Windows: двойной клик или "run.bat check" для проверки настроек
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\pip install -q -r requirements.txt
)
if not exist config.yaml (
  copy config.example.yaml config.yaml >nul
  echo Создан config.yaml - заполни его и запусти снова.
  pause
  exit /b 1
)
if not exist .env copy .env.example .env >nul
set PYTHONUTF8=1
.venv\Scripts\python -m platesh %*
pause
