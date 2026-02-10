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
if "%SKIP_UPDATE%"=="1" (
  echo [*] Auto-update disabled via SKIP_UPDATE=1
  goto :start_server
)

REM Warnung, falls lokale meshtastic.py die Paket-Importe überschattet
if exist "meshtastic.py" (
  echo [WARN] Es existiert eine lokale Datei meshtastic.py im Projektordner.
  echo [WARN] Das kann Paketimporte verhindern. Benuetze bitte ein virtuelles Environment oder benenne die Datei um.
)

REM Create virtualenv if not exists
set "VENV_DIR=.venv"
if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo [*] Erzeuge virtuelles Environment "%VENV_DIR%"...
  py -3 -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo [ERROR] Virtuelles Environment konnte nicht erstellt werden.
    pause
    exit /b 1
  )
) else (
  echo [OK] Virtualenv exists: %VENV_DIR%
)

REM Use venv python
set "PY=%VENV_DIR%\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Python im Virtualenv nicht gefunden: %PY%
  pause
  exit /b 1
)

REM Log file for installs
set "LOG=install_log.txt"
echo --- %date% %time% --- > "%LOG%"

REM Upgrade pip, setuptools, wheel
echo [*] Upgrading pip, setuptools, wheel...
"%PY%" -m pip install --upgrade pip setuptools wheel >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [ERROR] pip upgrade failed. See %LOG% for details.
  type "%LOG%"
  pause
  exit /b 1
)

REM Install/upgrade requirements (will upgrade packages to newer versions where possible)
if exist "requirements.txt" (
  echo [*] Installing/upgrading requirements from requirements.txt...
  "%PY%" -m pip install --upgrade -r requirements.txt >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo [ERROR] Installing requirements failed. See %LOG% for details.
    type "%LOG%"
    pause
    exit /b 1
  )
  echo [OK] Requirements installed/updated. See %LOG% for details.
) else (
  echo [WARN] requirements.txt not found; skipping pip install.
)

echo.

:start_server
REM Ermittle lokale IP mit Python-Skript (zuverlässiger als ipconfig parsing)
set "ip=127.0.0.1"
set "protocol=http"

REM Versuche IP mit Python zu ermitteln (für Zertifikat-Generierung)
if exist "%PY%" (
  for /f "delims=" %%i in ('"%PY%" get_local_ip.py 2^>nul') do set "ip=%%i"
)

REM Check for SSL certificates and optionally generate them
if exist "cert.pem" (
  if exist "key.pem" (
    set "protocol=https"
    echo [*] SSL certificates found - HTTPS enabled!
    echo [!] Camera access will work on all devices
    echo [!] You may need to accept the self-signed certificate in your browser
  )
) else (
  echo [WARN] No SSL certificates found - using HTTP
  echo [WARN] Camera access requires localhost or HTTPS
  echo.
  echo [*] Attempting to generate SSL certificate automatically...
  if exist "%PY%" (
    "%PY%" generate_cert.py %ip% >nul 2>&1
    if exist "cert.pem" (
      if exist "key.pem" (
        set "protocol=https"
        echo [OK] SSL certificate generated successfully!
        echo [!] HTTPS enabled - Camera will work on all devices!
      ) else (
        echo [WARN] Certificate generation failed - continuing with HTTP
        echo [INFO] To enable HTTPS: Run generate_ssl_cert.bat or generate_cert.py
      )
    ) else (
      echo [WARN] Certificate generation failed - continuing with HTTP
      echo [INFO] To enable HTTPS: Run generate_ssl_cert.bat or generate_cert.py
    )
  ) else (
    echo [INFO] To enable HTTPS: Run generate_ssl_cert.bat or generate_cert.py
  )
)

REM Display detected IP (already detected above for certificate generation)
echo [OK] Detected IP: %ip%
echo.

set "url=%protocol%://%ip%:8000/landing.html"
echo Starting API Server on %url%
echo.

REM Starte Server in neuem Fenster mit dem venv-Python
start "LPU5 API Server" cmd /k "%PY% api.py"

REM Warte kurz damit der Server starten kann
timeout /t 3 /nobreak >nul

REM Öffne Browser (optional)
start "" "%url%"

echo.
echo [OK] Server started and browser opened!
echo.
pause
endlocal