#!/usr/bin/env bash
# ============================================================
#  LPU5 Tactical – Linux/macOS Build Script
#  Erstellt eine eigenständige Binärdatei im dist/-Ordner.
# ============================================================
set -e

echo "[BUILD] Prüfe Python ..."
command -v python3 >/dev/null 2>&1 || { echo "[FEHLER] Python 3 nicht gefunden."; exit 1; }

echo "[BUILD] Installiere Abhängigkeiten ..."
pip3 install -r requirements_gui.txt

echo "[BUILD] Erstelle Binary mit PyInstaller ..."
pyinstaller \
    --onefile \
    --windowed \
    --name lpu5_gui \
    --add-data "index.html:." \
    start_gui.py

echo ""
echo "[FERTIG] Binary befindet sich in: dist/lpu5_gui"
