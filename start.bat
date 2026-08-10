@echo off
REM ===========================================================================
REM  FinOps AI Command Center - Windows one-shot setup and launch
REM
REM  Installs the backend and both frontends if they are not installed yet,
REM  creates and seeds the database on first run, then opens three windows:
REM  the API, the Execute app and the Platform Console.
REM
REM  Usage:   start.bat              normal run (installs only what is missing)
REM           start.bat reinstall    force a clean reinstall of dependencies
REM           start.bat reseed       rebuild the database from scratch
REM ===========================================================================

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "MODE=%~1"
set "PY=.venv\Scripts\python.exe"

echo.
echo ===========================================================
echo   FinOps AI Command Center
echo ===========================================================
echo.

REM --- 0. Sanity: are we in the right place and on the new layout? ----------
if not exist "backend\pyproject.toml" (
    echo [X] backend\pyproject.toml not found.
    echo     Run this file from the repository root.
    goto :fail
)
if not exist "apps\execute\package.json" (
    echo [X] apps\execute was not found - your checkout predates the two-app split.
    echo.
    echo     Pull first:
    echo         git pull origin claude/enterprise-ai-command-center-fuwcla
    echo.
    goto :fail
)

REM --- 1. Config check ------------------------------------------------------
if not exist ".env" (
    echo [X] No .env file. Copy the template and edit it:
    echo         copy .env.example .env
    goto :fail
)

REM A '#' only starts a comment in a .env file when whitespace precedes it, so
REM "DATABASE_URL=sqlite:///x##postgres://y" silently keeps the whole string.
findstr /R /C:"^DATABASE_URL=.*##" .env >nul 2>&1
if not errorlevel 1 (
    echo [X] DATABASE_URL in .env contains '##'.
    echo     Everything after it is part of the value, not a comment.
    echo     Set exactly:  DATABASE_URL=sqlite+aiosqlite:///./finops.db
    goto :fail
)

REM "$(openssl ...)" is shell syntax; in a .env file it is a literal string.
findstr /C:"JWT_SECRET=$(" .env >nul 2>&1
if not errorlevel 1 (
    echo [X] JWT_SECRET in .env is still the literal text "$(openssl rand -hex 32)".
    echo     Shell substitution does not run inside a .env file.
    echo     Generate one with:
    echo         python -c "import secrets; print(secrets.token_urlsafe(48))"
    goto :fail
)
findstr /C:"JWT_SECRET=change-me" .env >nul 2>&1
if not errorlevel 1 (
    echo [X] JWT_SECRET is still the placeholder from the template. Replace it.
    goto :fail
)

echo [1/6] Configuration looks sane.

REM --- 2. Python virtual environment ---------------------------------------
pushd backend

if /I "%MODE%"=="reinstall" (
    if exist ".venv" (
        echo       Removing the existing virtual environment...
        rmdir /s /q .venv
    )
)

if not exist "%PY%" (
    echo [2/6] Creating the Python virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [X] Could not create a virtual environment. Is Python 3.11+ installed
        echo     and on PATH?  Check with:  python --version
        popd
        goto :fail
    )
    set "FRESH_VENV=1"
) else (
    echo [2/6] Virtual environment already present.
)

if not exist "%PY%.installed" (
    echo [3/6] Installing backend dependencies - this takes a few minutes...
    "%PY%" -m pip install --upgrade pip --quiet
    "%PY%" -m pip install -e ".[dev,guardrails]"
    if errorlevel 1 (
        echo [X] Backend dependency installation failed.
        popd
        goto :fail
    )
    REM Marker so a later run skips the install without re-resolving every package.
    echo installed> "%PY%.installed"
) else (
    echo [3/6] Backend dependencies already installed.
)

REM --- 3. Database ----------------------------------------------------------
if /I "%MODE%"=="reseed" (
    if exist "finops.db" (
        echo       Deleting the existing database...
        del /q finops.db
    )
)

if not exist "finops.db" (
    echo [4/6] Creating the schema and seeding the platform...
    "%PY%" -m app.cli init-db
    if errorlevel 1 (
        echo [X] init-db failed. The message above says why - most often a value
        echo     in .env that cannot be parsed.
        popd
        goto :fail
    )
    echo       Loading the sample bank ^(24 customers^)...
    "%PY%" -m app.cli seed-banking --customers 24
    if errorlevel 1 (
        echo [X] seed-banking failed.
        popd
        goto :fail
    )
) else (
    echo [4/6] Database already exists - skipping seed. Use "start.bat reseed" to rebuild.
)

popd

REM --- 4. Frontend ----------------------------------------------------------
if /I "%MODE%"=="reinstall" (
    if exist "node_modules" (
        echo       Removing node_modules...
        rmdir /s /q node_modules
    )
)

if not exist "node_modules" (
    echo [5/6] Installing frontend dependencies - this takes a few minutes...
    call npm install
    if errorlevel 1 (
        echo [X] npm install failed. Is Node 20+ installed?  Check with:  node --version
        goto :fail
    )
) else (
    echo [5/6] Frontend dependencies already installed.
)

REM --- 5. Launch ------------------------------------------------------------
echo [6/6] Starting services in separate windows...
echo.

start "FinOps API"     cmd /k "cd /d "%~dp0backend" && .venv\Scripts\uvicorn.exe app.main:app --reload --port 8000"

REM Give the API a head start so the first frontend request does not race it.
timeout /t 6 /nobreak >nul

start "FinOps Execute" cmd /k "cd /d "%~dp0" && npm run dev:execute"
start "FinOps Console" cmd /k "cd /d "%~dp0" && npm run dev:console"

echo ===========================================================
echo   Running.
echo.
echo     API           http://localhost:8000
echo     API docs      http://localhost:8000/docs
echo     Execute app   http://localhost:5174
echo     Console app   http://localhost:5173
echo.
echo     Sign in with the BOOTSTRAP_ADMIN_EMAIL and
echo     BOOTSTRAP_ADMIN_PASSWORD values from your .env
echo.
echo   Close the three windows to stop everything.
echo ===========================================================
echo.
timeout /t 12 /nobreak >nul
goto :eof

:fail
echo.
echo Setup stopped. Nothing was started.
echo.
pause
exit /b 1
