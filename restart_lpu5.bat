@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

echo.
echo ========================================
echo   LPU5 TACTICAL TRACKER - RESTART
echo ========================================
echo.

REM Find and stop the running server
echo [*] Suche nach laufendem Server...
echo [*] Searching for running server...

REM Find Python process running api.py
for /f "tokens=2" %%a in ('tasklist /FI "IMAGENAME eq python.exe" /FO LIST ^| findstr /I "PID:"') do (
    set PID=%%a
    echo [*] Gefundener Prozess / Found process: %%a
    
    REM Check if this is our uvicorn process
    tasklist /FI "PID eq %%a" /V | findstr /I "api:app" >nul
    if not errorlevel 1 (
        echo [*] Stoppe Server / Stopping server (PID: %%a)...
        taskkill /PID %%a /F >nul 2>&1
        if errorlevel 1 (
            echo [WARN] Konnte Prozess nicht beenden / Could not stop process
        ) else (
            echo [OK] Server gestoppt / Server stopped
        )
    )
)

REM Check if port 8001 is still in use
netstat -ano | findstr ":8001.*LISTENING" >nul
if not errorlevel 1 (
    echo [WARN] Port 8001 ist noch in Verwendung / Port 8001 is still in use
    echo [*] Versuche alle Prozesse auf Port 8001 zu beenden...
    echo [*] Trying to stop all processes on port 8001...
    
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8001.*LISTENING"') do (
        echo [*] Beende Prozess / Stopping process: %%a
        taskkill /PID %%a /F >nul 2>&1
    )
)

REM Wait for port to be released
echo [*] Warte auf Freigabe von Port 8001...
echo [*] Waiting for port 8001 to be released...
timeout /t 3 /nobreak >nul

REM Verify port is free
netstat -ano | findstr ":8001.*LISTENING" >nul
if not errorlevel 1 (
    echo [ERROR] Port 8001 ist immer noch belegt!
    echo [ERROR] Port 8001 is still in use!
    echo [ERROR] Bitte pruefen Sie manuell:
    echo [ERROR] Please check manually:
    echo [ERROR]   netstat -ano ^| findstr :8001
    pause
    exit /b 1
)

echo [OK] Port 8001 ist frei / Port 8001 is free
echo.
echo [*] Starte Server neu...
echo [*] Restarting server...
echo.

REM Start the server using start_lpu5.bat
if exist "%~dp0start_lpu5.bat" (
    call "%~dp0start_lpu5.bat"
) else (
    echo [ERROR] start_lpu5.bat nicht gefunden in %~dp0
    echo [ERROR] start_lpu5.bat not found in %~dp0
    pause
    exit /b 1
)
