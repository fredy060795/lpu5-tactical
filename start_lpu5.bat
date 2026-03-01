@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

echo.
echo ========================================
echo   LPU5 TACTICAL TRACKER
echo   Start (with automatic dependency update)
echo ========================================
echo.

REM Optionally skip auto-update by setting SKIP_UPDATE=1 in environment
if "%SKIP_UPDATE%" == "1" (
    echo [*] Auto-update disabled via SKIP_UPDATE=1
    goto :start_server
)

REM Warning if local meshtastic.py shadows package imports
if exist "meshtastic.py" (
    echo [WARN] A local file meshtastic.py exists in the project directory.
    echo [WARN] This can prevent package imports. Please use a virtual environment.
)

REM Create virtualenv if not exists
set "VENV_DIR=.venv"
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [*] Creating virtual environment ".venv"...
    py -3 -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Could not create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [OK] Virtualenv exists: .venv
)

echo [*] Activating virtual environment...
call "%VENV_DIR%\Scriptsctivate.bat"

echo [*] Installing/updating dependencies...
pip install --upgrade pip >nul 2>&1
pip install -r requirements.txt

REM ── Hardware dependency checks ───────────────────────────────────────────
echo.
echo [*] Checking hardware dependencies...
set SDR_TOOLS_MISSING=0

for %%T in (rtl_tcp.exe rtl_power.exe rtl_test.exe rtl_fm.exe) do (
    where %%T >nul 2>&1
    if errorlevel 1 (
        echo [WARN] %%T not found in PATH
        set SDR_TOOLS_MISSING=1
    ) else (
        echo [OK] %%T found
    )
)

if "%SDR_TOOLS_MISSING%"=="1" (
    echo.
    echo [WARN] One or more RTL-SDR system tools are missing.
    echo [WARN] SDR features (spectrum view, audio streaming) will not be available until these tools are installed.
    echo.
    echo [INFO] Install RTL-SDR tools for Windows:
    echo [INFO]   Download from: https://osmocom.org/projects/rtl-sdr/wiki
    echo [INFO]   Extract and add the folder to your PATH.
    echo [INFO]   Then start: rtl_tcp.exe
    echo.
    echo [INFO] Check dependency status at runtime via:
    echo [INFO]   GET /api/dependencies/check
    echo.
)
REM ── End hardware dependency checks ──────────────────────────────────────

:start_server
echo.
echo [*] Starting server (api.py)...
echo [*] Press CTRL+C to stop
echo.
python api.py
pause
