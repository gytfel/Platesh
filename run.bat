@echo off
rem Запуск на Windows: двойной клик или .\run.bat в PowerShell. При первом запуске задаст вопросы и всё настроит.
rem   .\run.bat setup   - настроить заново
rem   .\run.bat check   - проверить настройки
rem   .\run.bat update  - скачать новую версию (настройки сохранятся)
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
rem Зависимости ставим заново, если изменился requirements.txt (копия лежит в .venv\.installed).
if exist .venv\.installed fc /b requirements.txt .venv\.installed >nul 2>nul && goto run
if exist .venv\Scripts\python.exe goto install

rem Ищем настоящий Python 3.10+: python (если добавлен в PATH) или py (ставится с python.org всегда).
set "PY="
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=py -3"
if not defined PY goto nopython
%PY% -m venv .venv || goto fail

:install
echo Устанавливаю зависимости (около минуты)...
.venv\Scripts\python -m pip install -q -r requirements.txt || goto fail
copy /y requirements.txt .venv\.installed >nul

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
