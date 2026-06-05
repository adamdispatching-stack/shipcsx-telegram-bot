@echo off
REM ============================================================
REM  Push this bot to a GitHub repository (Windows).
REM
REM  BEFORE running:
REM   1. Install Git:  https://git-scm.com/download/win
REM   2. Create a NEW, EMPTY repo on GitHub (no README/.gitignore)
REM      e.g. https://github.com/yourname/shipcsx-telegram-bot
REM   3. Copy its URL - you'll paste it below.
REM ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ===========================================================
echo   Push ShipCSX bot to GitHub
echo ===========================================================
echo.

REM --- Check Git ---------------------------------------------
where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Git is not installed.
    echo Install it from https://git-scm.com/download/win then re-run this file.
    pause
    exit /b 1
)

REM --- Ask for repo URL --------------------------------------
set "REPO="
set /p REPO="Paste your GitHub repo URL (https://github.com/you/repo.git): "
if "!REPO!"=="" (
    echo [ERROR] Repo URL cannot be empty.
    pause
    exit /b 1
)

REM --- Init repo if needed -----------------------------------
if not exist ".git" (
    git init
    git branch -M main
)

REM --- Set the remote (add or update) ------------------------
git remote get-url origin >nul 2>nul
if errorlevel 1 (
    git remote add origin "!REPO!"
) else (
    git remote set-url origin "!REPO!"
)

REM --- Commit everything -------------------------------------
git add -A
git commit -m "ShipCSX Telegram bot" 2>nul
if errorlevel 1 (
    echo (Nothing new to commit, or commit already exists - continuing.)
)

REM --- Push --------------------------------------------------
echo.
echo Pushing to !REPO! ...
echo (A browser/login window may appear so Git can authenticate with GitHub.)
git push -u origin main
if errorlevel 1 (
    echo.
    echo [ERROR] Push failed. Common causes:
    echo   - The GitHub repo is not empty ^(try: git pull origin main --rebase, then re-run^)
    echo   - You cancelled the GitHub login
    pause
    exit /b 1
)

echo.
echo ===========================================================
echo   Done! Code is on GitHub.
echo.
echo   NEXT - connect it to Railway:
echo   1. Go to https://railway.app  -^>  New Project
echo   2. Deploy from GitHub repo  -^>  pick this repo
echo   3. Open the service -^> Variables -^> add:
echo        BOT_TOKEN = your bot token
echo      ^(Do NOT set AUTHORIZED_CHAT_IDS - leaving it out lets everyone use it^)
echo   4. Railway builds the Dockerfile and starts the bot.
echo ===========================================================
echo.
echo   To push future changes, just run this file again.
echo.
pause
endlocal
