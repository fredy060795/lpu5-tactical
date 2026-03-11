# LPU5 Tactical – Desktop GUI

Eine eigenständige Python-Desktop-Anwendung, die alle HTML-Seiten des LPU5-Servers
in einem nativen Desktop-Fenster bündelt und per Netzwerk mit dem LPU5-Backend kommuniziert.

## Architektur

```
┌──────────────────────────────────┐
│  LPU5 Desktop GUI (diese App)    │
│  start_gui.py + PyWebView        │
│                                  │
│  ┌─────────────────────────┐     │
│  │  Natives Desktop-Fenster│     │
│  │  (HTML/JS/CSS der SPA)  │     │
│  └────────────┬────────────┘     │
└───────────────┼──────────────────┘
                │  HTTP / HTTPS
                ▼
┌──────────────────────────────────┐
│  LPU5-Server (Backend)           │
│  python api.py  →  Port 8101     │
│  Kein GUI-Overhead               │
└──────────────────────────────────┘
```

- **Backend** läuft separat (`python api.py`) und verarbeitet alle API-Anfragen.
- **Desktop-GUI** ist ein leichtgewichtiger PyWebView-Wrapper; keine eigene Server-Logik.
- Mehrere GUI-Clients können gleichzeitig mit demselben Backend verbunden sein.

---

## Voraussetzungen

| Komponente | Version |
|---|---|
| Python | 3.10 oder neuer |
| pywebview | ≥ 4.4 |
| Betriebssystem | Windows 10/11, Linux (GTK3/Qt), macOS 11+ |

### Systemabhängigkeiten

**Windows** – keine zusätzlichen Pakete nötig (Edge WebView2 ist vorinstalliert).

**Linux (Ubuntu/Debian)**:
```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
                 gir1.2-webkit2-4.0 libgtk-3-dev
```

**macOS** – keine zusätzlichen Pakete nötig.

---

## Installation

```bash
# Im gui/-Verzeichnis ausführen
pip install -r requirements_gui.txt
```

---

## Starten

### Variante A – Mit grafischer Verbindungsseite

```bash
python start_gui.py
```

Beim Start öffnet sich ein Fenster mit einem Eingabefeld für die Server-URL.
Trage die Adresse deines laufenden LPU5-Servers ein (z. B. `https://192.168.8.1:8101`)
und klicke **Verbinden**. Die komplette SPA des Servers wird dann im nativen Fenster geladen.

### Variante B – Direkt mit Server-URL

```bash
python start_gui.py --server https://192.168.8.1:8101
```

Die Verbindungsseite wird übersprungen; das Fenster öffnet sich sofort mit der Server-UI.

### Weitere Optionen

```
--server URL    Server-URL (leer = Eingabe über Verbindungsseite)
--width  PX     Fensterbreite in Pixel  (Standard: 1280)
--height PX     Fensterhöhe in Pixel   (Standard: 800)
```

---

## Backend separat starten

Der LPU5-Server läuft unabhängig vom GUI-Client:

```bash
# Im Projektstamm ausführen
python api.py
# oder via Start-Script:
start_lpu5.bat          # Windows
./start_lpu5.sh         # Linux
```

Der Server lauscht standardmäßig auf **Port 8101** (HTTP oder HTTPS mit SSL-Zertifikaten).

---

## Selbstsigniertes Zertifikat akzeptieren (HTTPS)

Startet der Server mit HTTPS (Standardfall), erscheint beim ersten Verbindungsaufbau
evtl. eine Browser-Zertifikatswarnung im PyWebView-Fenster.

**Windows (Edge WebView2):** Im Fenster auf „Erweitert → Weiter zu …" klicken.

**Alternativ:** `http://` statt `https://` verwenden, wenn der Server im HTTP-Modus läuft
(keine Zertifikatsdateien im Projektverzeichnis).

---

## Windows-EXE erstellen

```bat
cd gui
build_exe.bat
```

Die fertige EXE befindet sich anschließend in `gui/dist/lpu5_gui.exe`.
Die EXE ist standalone – kein Python auf dem Zielrechner erforderlich.

### Manuell mit PyInstaller

```bash
pyinstaller \
    --onefile \
    --windowed \
    --name lpu5_gui \
    --add-data "index.html;." \
    start_gui.py
```

> **Hinweis (Windows):** `--add-data` trennt Quelle und Ziel mit `;`.
> Auf Linux/macOS `:` verwenden.

---

## Linux-/macOS-Binary erstellen

```bash
cd gui
chmod +x build_exe.sh
./build_exe.sh
```

---

## Verzeichnisstruktur

```
gui/
├── start_gui.py         # Einstiegspunkt der Desktop-App
├── index.html           # Lokale Verbindungs-/Launcher-Seite
├── requirements_gui.txt # Python-Abhängigkeiten (pywebview, pyinstaller)
├── build_exe.bat        # Windows-Build-Script
├── build_exe.sh         # Linux/macOS-Build-Script
└── README.md            # Diese Datei
```

---

## Häufige Fehler

| Problem | Lösung |
|---|---|
| `ModuleNotFoundError: webview` | `pip install pywebview` ausführen |
| Fenster bleibt weiß | Verbindung zum Server prüfen; Server muss laufen |
| SSL-Fehler / Zertifikatswarnung | Zertifikat im Browser-Dialog akzeptieren oder HTTP verwenden |
| Linux: GTK-Fehler | Systemabhängigkeiten (s. o.) installieren |
| EXE startet nicht | `--windowed` entfernen um Konsolenausgabe zu sehen |
