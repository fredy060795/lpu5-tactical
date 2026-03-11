@echo off
REM ============================================================
REM  LPU5 Tactical – Windows EXE Build Script
REM  Erstellt eine eigenständige lpu5_gui.exe im dist/-Ordner.
REM ============================================================

echo [BUILD] Prüfe Python ...
where python >nul 2>&1 || (echo [FEHLER] Python nicht gefunden. Bitte Python 3.10+ installieren. & exit /b 1)

echo [BUILD] Installiere Abhängigkeiten ...
pip install -r requirements_gui.txt

echo [BUILD] Erstelle EXE mit PyInstaller ...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name lpu5_gui ^
    --add-data "index.html;." ^
    --icon="..\logo.png" ^
    start_gui.py

echo.
echo [FERTIG] EXE befindet sich in: dist\lpu5_gui.exe
pause
