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

REM Warnung, falls lokale meshtastic.py die Paket-Importe überschattet
if exist "meshtastic.py" (
    echo [WARN] Es existiert eine lokale Datei meshtastic.py im Projektordner.
    echo [WARN] Das kann Paketimporte verhindern. Bitte ein virtuelles Environment nutzen.
)

REM Create virtualenv if not exists
set "VENV_DIR=.venv"
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [*] Erzeuge virtuelles Environment ".venv"...
    py -3 -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Virtuelles Environment konnte nicht erstellt werden.
        pause
        exit /b 1
    )
) else (
    echo [OK] Virtualenv exists: .venv
)

echo [*] Aktiviere virtuelles Environment...
call "%VENV_DIR%\Scriptsctivate.bat"

echo [*] Installiere/Aktualisiere Abhaengigkeiten...
pip install --upgrade pip >nul 2>&1
pip install -r requirements.txt

:start_server
echo.
echo [*] Starte Server (api.py)...
echo [*] Druecke STRG+C zum Beenden
echo.
python api.py
pause
