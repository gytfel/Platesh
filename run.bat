@echo off
rem Запуск на Windows: двойной клик или .\run.bat в PowerShell. При первом запуске задаст вопросы и всё настроит.
rem   .\run.bat setup  - настроить заново
rem   .\run.bat check  - проверить настройки
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if exist .venv\.installed goto run

rem Ищем настоящий Python 3.10+: python (если добавлен в PATH) или py (ставится с python.org всегда).
set "PY="
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=py -3"
if not defined PY goto nopython

echo Устанавливаю зависимости (один раз, около минуты)...
if not exist .venv\Scripts\python.exe %PY% -m venv .venv || goto fail
.venv\Scripts\python -m pip install -q -r requirements.txt || goto fail
type nul > .venv\.installed

:run
.venv\Scripts\python -m platesh %*
pause
exit /b

:nopython
echo Не найден Python 3.10 или новее.
echo Установи его с https://www.python.org/downloads/ и запусти run.bat снова.
pause
exit /b 1

:fail
echo Не удалось установить зависимости. Проверь интернет и запусти run.bat снова.
pause
exit /b 1
