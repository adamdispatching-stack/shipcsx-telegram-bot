@echo off
REM ============================================================
REM  ShipCSX -> Telegram bot : one-shot Railway deploy (Windows)
REM  Double-click this file, or run it from a terminal.
REM ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ===========================================================
echo   ShipCSX Telegram Bot - Railway deployer
echo ===========================================================
echo.

REM --- 1. Check Node / npm (needed for the Railway CLI) -------
where npm >nul 2>nul
if errorlevel 1 (
    echo [ERROR] npm was not found.
    echo Install Node.js first from https://nodejs.org/ then re-run this file.
    echo.
    pause
    exit /b 1
)

REM --- 2. Ensure the Railway CLI is installed -----------------
where railway >nul 2>nul
if errorlevel 1 (
    echo Railway CLI not found. Installing it globally with npm...
    call npm install -g @railway/cli
    if errorlevel 1 (
        echo [ERROR] Could not install the Railway CLI.
        pause
        exit /b 1
    )
)

echo.
echo Railway CLI version:
call railway --version
echo.

REM --- 3. Log in (opens your browser) ------------------------
echo Logging in to Railway. A browser window will open - approve it there.
call railway whoami >nul 2>nul
if errorlevel 1 (
    call railway login
    if errorlevel 1 (
        echo [ERROR] Railway login failed.
        pause
        exit /b 1
    )
)
echo Logged in as:
call railway whoami
echo.

REM --- 4. Link or create a project ---------------------------
if not exist ".railway" (
    echo No Railway project linked yet.
    echo Choose: create a NEW project, or link to an EXISTING one.
    echo   - "railway init" creates a new project.
    echo   - "railway link" attaches this folder to one you already made.
    echo.
    set /p NEWPROJ="Create a NEW Railway project now? (Y/N): "
    if /I "!NEWPROJ!"=="Y" (
        call railway init
    ) else (
        call railway link
    )
    if errorlevel 1 (
        echo [ERROR] Could not set up the Railway project.
        pause
        exit /b 1
    )
)

REM --- 5. Collect environment variables ----------------------
echo.
echo --- Bot configuration ---
set "BOT_TOKEN="
set /p BOT_TOKEN="Paste your Telegram BOT_TOKEN: "
if "!BOT_TOKEN!"=="" (
    echo [ERROR] BOT_TOKEN cannot be empty.
    pause
    exit /b 1
)

set "AUTH=1042119341"
set /p AUTH="Authorized chat IDs [default 1042119341]: "
if "!AUTH!"=="" set "AUTH=1042119341"

echo.
echo Setting variables on Railway...
call railway variables --set "BOT_TOKEN=!BOT_TOKEN!" --set "AUTHORIZED_CHAT_IDS=!AUTH!"
if errorlevel 1 (
    echo [ERROR] Failed to set variables.
    pause
    exit /b 1
)

REM --- 6. Deploy ---------------------------------------------
echo.
echo Deploying to Railway (this builds the Docker image and may take a few minutes)...
call railway up
if errorlevel 1 (
    echo [ERROR] Deployment failed. Check the output above.
    pause
    exit /b 1
)

echo.
echo ===========================================================
echo   Done! Your bot is deploying.
echo   - Watch logs:        railway logs
echo   - Open dashboard:    railway open
echo   Then message your bot in Telegram and send /start
echo ===========================================================
echo.
pause
endlocal
