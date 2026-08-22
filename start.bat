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
REM
REM  Sign-in trouble? The seed hashes BOOTSTRAP_ADMIN_PASSWORD once, while the
REM  users table is still empty. Editing .env afterwards does not change the
REM  stored hash, so "start.bat reseed" is the fix. This script checks for that
REM  drift before launching and prints the working credentials at the end.
REM ===========================================================================

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "MODE=%~1"
set "PY=.venv\Scripts\python.exe"
set "PYOK=import sys;raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12),(3,13)) else 1)"

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

echo [1/7] Configuration looks sane.

REM --- 2. Pick an interpreter the dependencies actually support -------------
REM  NeMo Guardrails declares >=3.10,<3.14, and every implemented agent declares
REM  rails, so 3.14 is not merely untested - the install fails minutes in with a
REM  resolver error that never names the package. Prefer the newest supported.
set "PYEXE="
for %%V in (3.13 3.12 3.11) do (
    if not defined PYEXE (
        py -%%V -c "raise SystemExit(0)" >nul 2>&1
        if not errorlevel 1 set "PYEXE=py -%%V"
    )
)
if not defined PYEXE (
    python -c "%PYOK%" >nul 2>&1
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    echo [X] No supported Python found. This project needs 3.11, 3.12 or 3.13.
    echo.
    echo     Installed right now:
    py -0p 2>nul
    python --version 2>nul
    echo.
    echo     Python 3.14 will not work - NeMo Guardrails has no 3.14 release.
    echo     Install 3.13 from https://www.python.org/downloads/ and re-run.
    goto :fail
)
for /f "delims=" %%A in ('%PYEXE% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%A"
echo [2/7] Using Python !PYVER! ^(!PYEXE!^).

REM --- 3. Python virtual environment ----------------------------------------
pushd backend

if /I "%MODE%"=="reinstall" (
    if exist ".venv" (
        echo       Removing the existing virtual environment...
        rmdir /s /q .venv
    )
)

REM A venv built by a different interpreter keeps working until an import of a
REM compiled wheel fails - the ModuleNotFoundError names pydantic_core, not the
REM version mismatch that caused it. Check both, and say which.
if exist "%PY%" (
    "%PY%" -c "%PYOK%" >nul 2>&1
    if errorlevel 1 (
        echo       The existing .venv runs an unsupported Python. Rebuilding it...
        rmdir /s /q .venv
    )
)
if exist "%PY%" (
    if exist "%PY%.installed" (
        "%PY%" -c "import pydantic_core" >nul 2>&1
        if errorlevel 1 (
            echo       The existing .venv has broken compiled packages. Rebuilding it...
            rmdir /s /q .venv
        )
    )
)

if not exist "%PY%" (
    echo [3/7] Creating the Python virtual environment...
    %PYEXE% -m venv .venv
    if errorlevel 1 (
        echo [X] Could not create a virtual environment.
        echo     If the error mentions copying venvlauncher.exe, an antivirus or an
        echo     open file is holding .venv - close any running API window, then:
        echo         rmdir /s /q backend\.venv
        popd
        goto :fail
    )
) else (
    echo [3/7] Virtual environment already present.
)

if not exist "%PY%.installed" (
    echo [4/7] Installing backend dependencies - this takes a few minutes...
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
    echo [4/7] Backend dependencies already installed.
)

REM --- 4. Database ----------------------------------------------------------
if /I "%MODE%"=="reseed" (
    if exist "finops.db" (
        echo       Deleting the existing database...
        del /q finops.db
    )
)

if not exist "finops.db" goto :seed

REM The file existing does not mean the seed finished, nor that its hashed
REM password still matches .env. doctor exits 2 for unseeded, 1 for drifted.
"%PY%" scripts\doctor.py --quiet
if errorlevel 2 goto :seed
if errorlevel 1 (
    echo.
    "%PY%" scripts\doctor.py
    popd
    goto :fail
)
echo [5/7] Database already seeded, and its password matches .env.
goto :dbready

:seed
echo [5/7] Creating the schema and seeding the platform...
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

:dbready
popd

REM --- 5. Frontend ----------------------------------------------------------
where node >nul 2>&1
if errorlevel 1 (
    echo [X] Node is not on PATH. Install Node 20 or newer from https://nodejs.org
    goto :fail
)

if /I "%MODE%"=="reinstall" (
    if exist "node_modules" (
        echo       Removing node_modules...
        rmdir /s /q node_modules
    )
)

if not exist "node_modules" (
    echo [6/7] Installing frontend dependencies - this takes a few minutes...
    call npm install
    if errorlevel 1 (
        echo [X] npm install failed. Is Node 20+ installed?  Check with:  node --version
        goto :fail
    )
) else (
    echo [6/7] Frontend dependencies already installed.
)

REM --- 6. Launch ------------------------------------------------------------
echo [7/7] Starting services in separate windows...
echo.

REM Hyper-V reserves blocks of TCP ports, so 8000 can refuse to bind with WinError 10013
REM while netstat shows it free. Ask for a port that actually binds before opening a window
REM that would only print the error and sit there. Set FINOPS_API_PORT to pin one.
if not defined FINOPS_API_PORT set "FINOPS_API_PORT=8000"
set "API_PORT=%FINOPS_API_PORT%"
for /f "delims=" %%P in ('cd /d "%~dp0backend" ^&^& "%PY%" scripts\pick_port.py %FINOPS_API_PORT%') do set "API_PORT=%%P"

REM The dev servers proxy /api to the API, so the port has to follow it or every request 404s.
set "VITE_API_TARGET=http://localhost:%API_PORT%"

start "FinOps API"     cmd /k "cd /d "%~dp0backend" && .venv\Scripts\uvicorn.exe app.main:app --reload --port %API_PORT%"

REM Give the API a head start so the first frontend request does not race it.
timeout /t 6 /nobreak >nul

start "FinOps Execute" cmd /k "cd /d "%~dp0" && set "VITE_API_TARGET=%VITE_API_TARGET%" && npm run dev:execute"
start "FinOps Console" cmd /k "cd /d "%~dp0" && set "VITE_API_TARGET=%VITE_API_TARGET%" && npm run dev:console"

echo ===========================================================
echo   Running.
echo.
echo     API           http://localhost:%API_PORT%
echo     API docs      http://localhost:%API_PORT%/docs
echo     Execute app   http://localhost:5174
echo     Console app   http://localhost:5173
echo.
echo   Sign in through either front end, not against the API port.
echo ===========================================================
echo.

REM Print the credentials this database actually accepts, rather than naming
REM the .env key and leaving the reader to guess whether it was ever applied.
pushd backend
"%PY%" scripts\doctor.py
popd

timeout /t 12 /nobreak >nul
goto :eof

:fail
echo.
echo Setup stopped. Nothing was started.
echo.
pause
exit /b 1
