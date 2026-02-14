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
echo [*] Searching for running server...

REM Find Python process running api.py
for /f "tokens=2" %%a in ('tasklist /FI "IMAGENAME eq python.exe" /FO LIST ^| findstr /I "PID:"') do (
    set PID=%%a
    echo [*] Found process: %%a
    
    REM Check if this is our uvicorn process
    tasklist /FI "PID eq %%a" /V | findstr /I "api:app" >nul
    if not errorlevel 1 (
        echo [*] Stopping server (PID: %%a)...
        taskkill /PID %%a /F >nul 2>&1
        if errorlevel 1 (
            echo [WARN] Could not stop process
        ) else (
            echo [OK] Server stopped
        )
    )
)

REM Check if port 8001 is still in use
netstat -ano | findstr ":8001.*LISTENING" >nul
if not errorlevel 1 (
    echo [WARN] Port 8001 is still in use
    echo [*] Trying to stop all processes on port 8001...
    
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8001.*LISTENING"') do (
        echo [*] Stopping process: %%a
        taskkill /PID %%a /F >nul 2>&1
    )
)

REM Wait for port to be released
echo [*] Waiting for port 8001 to be released...
timeout /t 3 /nobreak >nul

REM Verify port is free
netstat -ano | findstr ":8001.*LISTENING" >nul
if not errorlevel 1 (
    echo [ERROR] Port 8001 is still in use!
    echo [ERROR] Please check manually:
    echo [ERROR]   netstat -ano ^| findstr :8001
    pause
    exit /b 1
)

echo [OK] Port 8001 is free
echo.
echo [*] Restarting server...
echo.

REM Start the server using start_lpu5.bat
if exist "%~dp0start_lpu5.bat" (
    call "%~dp0start_lpu5.bat"
) else (
    echo [ERROR] start_lpu5.bat not found in %~dp0
    pause
    exit /b 1
)
