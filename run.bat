@echo off
rem Запуск на Windows: двойной клик или run.bat в терминале. При первом запуске задаст вопросы и всё настроит.
rem   run.bat setup  - настроить заново
rem   run.bat check  - проверить настройки
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\.installed (
  echo Устанавливаю зависимости ^(один раз^)...
  if not exist .venv python -m venv .venv || goto :fail
  .venv\Scripts\pip install -q -r requirements.txt || goto :fail
  type nul > .venv\.installed
)
set PYTHONUTF8=1
.venv\Scripts\python -m platesh %*
pause
exit /b

:fail
echo Не удалось установить зависимости. Проверь, что установлен Python 3.10+ и есть интернет.
pause
exit /b 1
