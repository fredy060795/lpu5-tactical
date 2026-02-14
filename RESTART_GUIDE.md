# LPU5 Tactical System - Restart Guide

## Übersicht / Overview

Dieses Dokument beschreibt, wie man das LPU5 Tactical System neu startet, nachdem Anpassungen vorgenommen wurden.

This document describes how to restart the LPU5 Tactical system after making adjustments.

---

## Schnellstart / Quick Start

### Linux/Unix/macOS

```bash
# System neu starten / Restart system
./restart_lpu5.sh

# Oder manuell starten / Or start manually
./start_lpu5.sh
```

### Windows

```cmd
REM System neu starten / Restart system
restart_lpu5.bat

REM Oder manuell starten / Or start manually
start_lpu5.bat
```

---

## Wann ist ein Neustart erforderlich? / When is a Restart Required?

### Immer neu starten nach / Always restart after:

1. **Konfigurationsänderungen / Configuration Changes**
   - Änderungen in `config.json`
   - Änderungen in `api.py`
   - Änderungen in Datenbank-Modellen (`models.py`)
   - SSL-Zertifikat-Aktualisierungen

2. **Python-Abhängigkeiten / Python Dependencies**
   - Änderungen in `requirements.txt`
   - Installation neuer Python-Pakete
   - Update bestehender Pakete

3. **Backend-Code-Änderungen / Backend Code Changes**
   - Änderungen in `api.py`
   - Änderungen in Service-Dateien (`meshtastic_gateway_service.py`, etc.)
   - Änderungen in `database.py` oder `models.py`

4. **Datenbank-Migrationen / Database Migrations**
   - Nach Ausführung von `migrate_db.py`
   - Nach Schema-Änderungen

### Kein Neustart erforderlich / No restart required:

1. **Frontend-Änderungen / Frontend Changes**
   - HTML-Dateien (nur Browser-Refresh)
   - JavaScript-Dateien (nur Browser-Refresh)
   - CSS-Dateien (nur Browser-Refresh)

2. **Statische Dateien / Static Files**
   - Bilder, Icons
   - Client-seitige Konfigurationen

---

## Detaillierte Anweisungen / Detailed Instructions

### 1. Graceful Restart (Empfohlen / Recommended)

#### Linux/Unix/macOS

```bash
# Prüfen ob der Server läuft / Check if server is running
lsof -i :8101

# Server beenden und neu starten / Stop and restart server
./restart_lpu5.sh
```

Das `restart_lpu5.sh` Skript:
- Findet den laufenden Server-Prozess
- Beendet ihn sanft (SIGTERM)
- Wartet bis der Port frei ist
- Startet den Server neu

The `restart_lpu5.sh` script:
- Finds the running server process
- Stops it gracefully (SIGTERM)
- Waits until the port is free
- Restarts the server

#### Windows

```cmd
REM Verwenden Sie restart_lpu5.bat / Use restart_lpu5.bat
restart_lpu5.bat
```

### 2. Manueller Neustart / Manual Restart

#### Schritt 1: Server stoppen / Stop Server

**Linux/Unix/macOS:**
```bash
# Finde den Prozess / Find the process
lsof -i :8101

# Oder mit ps
ps aux | grep "uvicorn api:app"

# Beende den Prozess / Kill the process
kill <PID>

# Oder erzwinge Beendigung / Or force kill
kill -9 <PID>
```

**Windows:**
```cmd
REM Finde den Prozess / Find the process
netstat -ano | findstr :8101

REM Beende den Prozess / Kill the process
taskkill /PID <PID> /F
```

#### Schritt 2: Server starten / Start Server

**Linux/Unix/macOS:**
```bash
./start_lpu5.sh
```

**Windows:**
```cmd
start_lpu5.bat
```

### 3. Systemd Service Neustart / Systemd Service Restart

Wenn als systemd Service installiert / If installed as systemd service:

```bash
# Status prüfen / Check status
sudo systemctl status lpu5-tactical

# Neustarten / Restart
sudo systemctl restart lpu5-tactical

# Logs anzeigen / View logs
sudo journalctl -u lpu5-tactical -f

# Service neu laden nach Dateiänderungen / Reload after file changes
sudo systemctl daemon-reload
sudo systemctl restart lpu5-tactical
```

---

## Häufige Szenarien / Common Scenarios

### Szenario 1: Konfiguration geändert / Configuration Changed

```bash
# 1. Bearbeite config.json
nano config.json

# 2. Starte neu / Restart
./restart_lpu5.sh

# 3. Prüfe ob der Server läuft / Check if server is running
curl -k https://localhost:8101/api/status
```

### Szenario 2: Python-Pakete aktualisiert / Python Packages Updated

```bash
# 1. Aktualisiere requirements.txt
nano requirements.txt

# 2. Installiere Abhängigkeiten / Install dependencies
source .venv/bin/activate
pip install -r requirements.txt

# 3. Starte neu / Restart
./restart_lpu5.sh
```

### Szenario 3: Code-Änderungen / Code Changes

```bash
# 1. Ändere Python-Code / Modify Python code
nano api.py

# 2. Teste Syntax (optional) / Test syntax (optional)
python3 -m py_compile api.py

# 3. Starte neu / Restart
./restart_lpu5.sh

# 4. Überwache Logs / Monitor logs
tail -f /var/log/lpu5-tactical.log
```

### Szenario 4: Datenbank-Migration / Database Migration

```bash
# 1. Führe Migration aus / Run migration
python3 migrate_db.py

# 2. Starte neu / Restart
./restart_lpu5.sh

# 3. Prüfe Datenbankverbindung / Check database connection
sqlite3 tactical.db "SELECT COUNT(*) FROM users;"
```

### Szenario 5: SSL-Zertifikate erneuert / SSL Certificates Renewed

```bash
# 1. Ersetze Zertifikate / Replace certificates
cp /path/to/new/cert.pem ./
cp /path/to/new/key.pem ./

# 2. Setze Berechtigungen / Set permissions
chmod 600 key.pem
chmod 644 cert.pem

# 3. Starte neu / Restart
./restart_lpu5.sh
```

---

## Fehlerbehebung / Troubleshooting

### Problem: Port bereits in Verwendung / Port already in use

**Symptom:**
```
[ERROR] Port 8101 is already in use
```

**Lösung / Solution:**
```bash
# Finde und beende den Prozess / Find and kill the process
lsof -i :8101
kill <PID>

# Oder verwende restart_lpu5.sh / Or use restart_lpu5.sh
./restart_lpu5.sh
```

### Problem: Server startet nicht / Server won't start

**Symptom:**
```
[ERROR] Failed to start server
```

**Debugging-Schritte / Debugging steps:**

1. **Prüfe Python-Version / Check Python version:**
   ```bash
   python3 --version  # Sollte >= 3.8 sein / Should be >= 3.8
   ```

2. **Prüfe Abhängigkeiten / Check dependencies:**
   ```bash
   source .venv/bin/activate
   pip list
   ```

3. **Prüfe Konfiguration / Check configuration:**
   ```bash
   cat config.json | jq  # Sollte valides JSON sein / Should be valid JSON
   ```

4. **Prüfe SSL-Zertifikate / Check SSL certificates:**
   ```bash
   openssl x509 -in cert.pem -text -noout
   ```

5. **Starte im Debug-Modus / Start in debug mode:**
   ```bash
   python3 -m uvicorn api:app --host 0.0.0.0 --port 8101 --reload
   ```

### Problem: Virtuelle Umgebung fehlt / Virtual environment missing

**Symptom:**
```
[ERROR] .venv/bin/activate not found
```

**Lösung / Solution:**
```bash
# Erstelle virtuelle Umgebung neu / Recreate virtual environment
python3 -m venv .venv

# Aktiviere / Activate
source .venv/bin/activate

# Installiere Abhängigkeiten / Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Starte / Start
./start_lpu5.sh
```

### Problem: Abhängigkeiten fehlen / Dependencies missing

**Symptom:**
```
ModuleNotFoundError: No module named 'fastapi'
```

**Lösung / Solution:**
```bash
source .venv/bin/activate
pip install -r requirements.txt
./restart_lpu5.sh
```

### Problem: Datenbank gesperrt / Database locked

**Symptom:**
```
sqlite3.OperationalError: database is locked
```

**Lösung / Solution:**
```bash
# Beende alle Prozesse die auf DB zugreifen / Kill all processes accessing DB
fuser tactical.db
kill <PID>

# Oder lösche Sperrdatei / Or delete lock file
rm tactical.db-journal

# Starte neu / Restart
./restart_lpu5.sh
```

---

## Überwachung nach Neustart / Monitoring After Restart

### Logs prüfen / Check logs

**Linux/Unix/macOS:**
```bash
# Systemd logs
sudo journalctl -u lpu5-tactical -f

# Datei-basierte Logs / File-based logs
tail -f /var/log/lpu5-tactical.log

# Python uvicorn logs direkt / Python uvicorn logs directly
# (wenn im Vordergrund gestartet / if started in foreground)
```

**Windows:**
```cmd
REM Prüfe Konsolenausgabe / Check console output
REM Oder Event Viewer für systemd-artige Services
```

### API-Status prüfen / Check API Status

```bash
# Einfacher Health-Check / Simple health check
curl -k https://localhost:8101/api/status

# Oder mit jq für formatierte Ausgabe / Or with jq for formatted output
curl -k https://localhost:8101/api/status | jq

# HTTP-Status-Code prüfen / Check HTTP status code
curl -k -o /dev/null -s -w "%{http_code}\n" https://localhost:8101/api/status
```

### Verbindungen prüfen / Check Connections

```bash
# Aktive Verbindungen / Active connections
lsof -i :8101

# Oder mit netstat
netstat -an | grep 8101

# Anzahl der Verbindungen / Number of connections
lsof -i :8101 | wc -l
```

### Performance überwachen / Monitor Performance

```bash
# CPU und RAM Nutzung / CPU and RAM usage
top -p $(pgrep -f "uvicorn api:app")

# Oder htop für bessere Visualisierung / Or htop for better visualization
htop -p $(pgrep -f "uvicorn api:app")

# Systemressourcen / System resources
free -h
df -h
```

---

## Automatischer Neustart / Automatic Restart

### Mit systemd (Empfohlen für Produktion / Recommended for Production)

Erstellen Sie einen systemd Service / Create a systemd service:

```bash
sudo nano /etc/systemd/system/lpu5-tactical.service
```

Inhalt / Content:
```ini
[Unit]
Description=LPU5 Tactical Tracker
After=network.target

[Service]
Type=simple
User=tactical
WorkingDirectory=/opt/lpu5-tactical
ExecStart=/opt/lpu5-tactical/.venv/bin/python3 -m uvicorn api:app --host 0.0.0.0 --port 8101 --ssl-keyfile key.pem --ssl-certfile cert.pem
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Aktivieren / Enable:
```bash
sudo systemctl daemon-reload
sudo systemctl enable lpu5-tactical
sudo systemctl start lpu5-tactical
```

### Mit Watchdog-Timer

Erstellen Sie ein Überwachungsskript / Create a monitoring script:

```bash
#!/bin/bash
# watch_lpu5.sh

while true; do
  if ! curl -k -f https://localhost:8101/api/status > /dev/null 2>&1; then
    echo "$(date): Server nicht erreichbar, starte neu..."
    echo "$(date): Server not reachable, restarting..."
    ./restart_lpu5.sh
  fi
  sleep 60
done
```

---

## Best Practices

### Vor dem Neustart / Before Restart

1. ✅ **Sicherung erstellen / Create backup**
   ```bash
   cp tactical.db tactical.db.backup
   tar -czf config_backup.tar.gz config.json *.pem
   ```

2. ✅ **Änderungen testen / Test changes**
   ```bash
   python3 -m py_compile api.py
   python3 -c "import json; json.load(open('config.json'))"
   ```

3. ✅ **Benutzer informieren / Notify users**
   - Bei geplanter Wartung / For planned maintenance
   - Kurze Ausfallzeit ankündigen / Announce brief downtime

### Während des Neustarts / During Restart

1. ✅ **Graceful Shutdown verwenden / Use graceful shutdown**
   - Gibt Zeit für aktive Verbindungen / Gives time for active connections
   - Verhindert Datenverlust / Prevents data loss

2. ✅ **Port-Freigabe prüfen / Verify port release**
   ```bash
   while lsof -i :8101 > /dev/null; do sleep 1; done
   echo "Port frei / Port free"
   ```

### Nach dem Neustart / After Restart

1. ✅ **Funktionalität testen / Test functionality**
   ```bash
   curl -k https://localhost:8101/api/status
   curl -k https://localhost:8101/api/map_markers
   ```

2. ✅ **Logs überwachen / Monitor logs**
   ```bash
   tail -f logs/*.log
   ```

3. ✅ **Performance prüfen / Check performance**
   ```bash
   time curl -k https://localhost:8101/api/status
   ```

---

## Checkliste für Neustart / Restart Checklist

### Vor dem Neustart / Before Restart
- [ ] Backup der Datenbank erstellt / Database backup created
- [ ] Konfiguration geprüft / Configuration verified
- [ ] Abhängigkeiten aktualisiert / Dependencies updated
- [ ] Benutzer informiert / Users notified

### Neustart durchführen / Perform Restart
- [ ] Alten Prozess gestoppt / Old process stopped
- [ ] Port freigegeben / Port released
- [ ] Neuen Prozess gestartet / New process started
- [ ] Startmeldung bestätigt / Startup message confirmed

### Nach dem Neustart / After Restart
- [ ] API-Endpunkte getestet / API endpoints tested
- [ ] WebSocket-Verbindung geprüft / WebSocket connection checked
- [ ] Frontend lädt korrekt / Frontend loads correctly
- [ ] Logs auf Fehler überprüft / Logs checked for errors
- [ ] Performance normal / Performance normal

---

## Support und Hilfe / Support and Help

### Dokumentation / Documentation
- [README.md](README.md) - Projekt-Übersicht / Project overview
- [DEPLOYMENT.md](DEPLOYMENT.md) - Deployment-Anleitung / Deployment guide
- [QUICKSTART.md](QUICKSTART.md) - Schnellstart / Quick start

### Kontakt / Contact
- GitHub Issues: https://github.com/fredy060795/lpu5-tactical/issues
- E-Mail: [Ihre Support-E-Mail / Your support email]

---

**Hinweis:** Stellen Sie sicher, dass Sie Backups vor größeren Änderungen erstellen!

**Note:** Make sure to create backups before major changes!
