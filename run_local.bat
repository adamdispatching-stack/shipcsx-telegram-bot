@echo off
REM ============================================================
REM  Run the ShipCSX -> Telegram bot LOCALLY for testing.
REM  Requires Python 3.10+ installed and on PATH.
REM  (Don't run this at the same time as the Railway deploy -
REM   Telegram only allows one running copy per bot token.)
REM ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found. Install it from https://www.python.org/
    pause
    exit /b 1
)

echo Creating virtual environment (first run only)...
if not exist ".venv" (
    python -m venv .venv
)
call ".venv\Scripts\activate.bat"

echo Installing dependencies...
pip install --quiet -r requirements.txt
python -m playwright install chromium

set "BOT_TOKEN="
set /p BOT_TOKEN="Paste your Telegram BOT_TOKEN: "
if "!BOT_TOKEN!"=="" (
    echo [ERROR] BOT_TOKEN cannot be empty.
    pause
    exit /b 1
)
set "AUTHORIZED_CHAT_IDS=1042119341"
set /p AUTHORIZED_CHAT_IDS="Authorized chat IDs [default 1042119341]: "
if "!AUTHORIZED_CHAT_IDS!"=="" set "AUTHORIZED_CHAT_IDS=1042119341"

echo.
echo Starting bot... (press Ctrl+C to stop)
python bot.py

pause
endlocal
