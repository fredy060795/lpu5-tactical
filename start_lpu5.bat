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
call "%VENV_DIR%\Scripts\activate.bat"

echo [*] Installing/updating core dependencies...
pip install --upgrade pip >nul 2>&1
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install core dependencies.
    pause
    exit /b 1
)
echo [OK] Core dependencies installed

echo [*] Installing optional SDR dependencies: pyrtlsdr, numpy ...
pip install "pyrtlsdr>=0.3.0" "numpy>=1.24.0" >nul 2>&1
if errorlevel 1 (
    echo [WARN] Optional SDR packages could not be installed.
    echo [WARN] SDR features will not be available. The server will still start.
    echo [WARN] To install manually later: pip install pyrtlsdr numpy
) else (
    echo [OK] Optional SDR dependencies installed
)

REM ── Hardware dependency checks ───────────────────────────────────────────
echo.
echo [*] Checking hardware dependencies...
set SDR_TOOLS_MISSING=0

for %%T in (rtl_tcp.exe rtl_power.exe rtl_test.exe rtl_fm.exe) do (
    where %%T >nul 2>&1
    if errorlevel 1 (
        if exist "%~dp0%%T" (
            echo [OK] %%T found in project directory
        ) else (
            echo [WARN] %%T not found in PATH or project directory
            set SDR_TOOLS_MISSING=1
        )
    ) else (
        echo [OK] %%T found
    )
)

if "%SDR_TOOLS_MISSING%"=="1" (
    echo.
    echo [WARN] One or more RTL-SDR system tools are missing.
    echo [WARN] SDR features (spectrum view, audio streaming) may be limited until these tools are installed.
    echo.
    echo [INFO] Install RTL-SDR tools for Windows:
    echo [INFO]   Download from: https://github.com/rtlsdrblog/rtl-sdr-blog/releases
    echo [INFO]   Extract the x64 folder and copy rtl_tcp.exe, rtlsdr.dll, libusb-1.0.dll
    echo [INFO]   into this project directory (%~dp0)
    echo [INFO]   Then install the WinUSB driver with Zadig: https://zadig.akeo.ie/
    echo [INFO]   The server will auto-start rtl_tcp when needed.
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
