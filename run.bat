@echo off
rem Запуск на Windows: двойной клик или "run.bat check" для проверки настроек
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\.installed (
  echo Устанавливаю зависимости ^(один раз^)...
  if not exist .venv python -m venv .venv || goto :fail
  .venv\Scripts\pip install -q -r requirements.txt || goto :fail
  type nul > .venv\.installed
)
if not exist config.yaml (
  copy config.example.yaml config.yaml >nul
  if not exist .env copy .env.example .env >nul
  echo Созданы config.yaml и .env - впиши в них токены и настройки, затем запусти снова.
  pause
  exit /b 1
)
if not exist .env copy .env.example .env >nul
set PYTHONUTF8=1
.venv\Scripts\python -m platesh %*
pause
exit /b

:fail
echo Не удалось установить зависимости. Проверь, что установлен Python 3.10+ и есть интернет.
pause
exit /b 1
