@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo.
echo ========================================
echo   LPU5 TACTICAL TRACKER - CLIENT
echo   Standalone Desktop-Anwendung (Full UI)
echo ========================================
echo.

REM ── Python erkennen ──────────────────────────────────────────
set "PYTHON_CMD="
where py >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
) else (
    where python3 >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_CMD=python3"
    ) else (
        where python >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_CMD=python"
        )
    )
)

if "!PYTHON_CMD!" == "" (
    echo [FEHLER] Python 3 wurde nicht gefunden.
    echo          Bitte installieren: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

REM ── Python-Version pruefen ───────────────────────────────────
echo [*] Python: !PYTHON_CMD!
!PYTHON_CMD! --version 2>&1

REM ── Virtuelle Umgebung ───────────────────────────────────────
set "VENV_DIR=.venv_client"

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [*] Erstelle virtuelle Umgebung...
    !PYTHON_CMD! -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [FEHLER] Virtuelle Umgebung konnte nicht erstellt werden.
        pause
        exit /b 1
    )
    echo [OK] Virtuelle Umgebung erstellt: %VENV_DIR%
)

call "%VENV_DIR%\Scripts\activate.bat"

REM ── Abhaengigkeiten installieren ─────────────────────────────
echo [*] Pruefe Abhaengigkeiten...
pip show pywebview >nul 2>&1
if errorlevel 1 (
    echo [*] Installiere pywebview...
    pip install --upgrade pip >nul 2>&1
    pip install pywebview 2>&1
    if errorlevel 1 (
        echo [FEHLER] pywebview konnte nicht installiert werden.
        echo          Versuchen Sie: pip install pywebview[cef]
        pause
        exit /b 1
    )
    echo [OK] pywebview installiert
) else (
    echo [OK] pywebview vorhanden
)

pip show pyserial >nul 2>&1
if errorlevel 1 (
    echo [*] Installiere pyserial (COM-Port Zugriff)...
    pip install pyserial 2>&1
    if errorlevel 1 (
        echo [WARNUNG] pyserial konnte nicht installiert werden.
        echo           COM-Port-Scan wird nicht verfuegbar sein.
    ) else (
        echo [OK] pyserial installiert
    )
) else (
    echo [OK] pyserial vorhanden
)

REM ── Dateien pruefen ──────────────────────────────────────────
if not exist "index.html" (
    echo [FEHLER] index.html nicht gefunden!
    echo          Stellen Sie sicher, dass dieses Skript im
    echo          LPU5 Projektverzeichnis liegt.
    pause
    exit /b 1
)

if not exist "LPU5.py" (
    echo [FEHLER] LPU5.py nicht gefunden!
    pause
    exit /b 1
)

REM ── Client starten ───────────────────────────────────────────
echo.
echo ========================================
echo   Starte LPU5 Desktop Client...
echo   Vollstaendige UI ohne Server.
echo   Fenster schliessen zum Beenden.
echo ========================================
echo.

python LPU5.py %*

echo.
echo [*] LPU5 Client beendet.
pause
