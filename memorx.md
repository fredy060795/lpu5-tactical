# MEMORX – LPU-5 System Gedächtnis

## Systemübersicht

**LPU-5 (Tactical Battle Tracker)** ist eine taktische Echtzeit-Lagebildplattform für:
- Einheitenverfolgung & Koordination (Positionen, Status, Geräte)
- Missionsmanagement (Erstellen, Zuweisen, Verfolgen)
- Kartenüberlagerungen & taktische Markierungen (Zeichnungen, Marker, Overlays, Symbole)
- Mesh-Netzwerk-Integration (Meshtastic-Geräte)
- Autonome Operationen (Regelbasierte Automatisierung mit Geofencing und CoT-Protokoll)
- Chat & Echtzeit-Updates (WebSocket-basierte Kommunikation)

**Tech-Stack:** FastAPI (Python), SQLite/SQLAlchemy, WebSockets, HTML5 Frontend, Progressive Web App (PWA)

---

## Architektur – Zwei-Prozess-Modell

```
┌─────────────────────────────────────────────────────────────────┐
│                      WEB-BROWSER (Clients)                      │
│  index.html, admin.html, admin_map.html, mission.html,         │
│  stream.html, network.html, overview.html, statistics.html     │
└───────────────────────┬─────────────────────────────────────────┘
                        │ HTTP REST + WebSocket
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│      HAUPT-API-SERVER (api.py – Port 8101)                      │
│                                                                  │
│  ├─ REST-Endpunkte (CRUD: User, Marker, Missions, Chat, …)     │
│  ├─ Authentifizierung (JWT Token, Sessions, SHA-256 Hashing)    │
│  ├─ Statische Dateien (/landing.html, /admin.html, etc.)        │
│  ├─ WebSocket-Fallback (falls Data-Server nicht verfügbar)      │
│  │                                                               │
│  └─ Autonome Systeme:                                            │
│     ├─ GeofencingManager (Zonenüberwachung)                      │
│     ├─ AutonomousEngine (Regeln & Trigger)                       │
│     ├─ CoTProtocolHandler (ATAK/WINTAK-Integration)              │
│     └─ ConnectionManager (WebSocket-Subscriptions)               │
│                                                                  │
│  Datenbank: SQLite (tactical.db) via SQLAlchemy ORM              │
└───────────────────────┬──────────────────────────────────────────┘
                        │ HTTP POST → /api/broadcast
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│     DATEN-SERVER (data_server.py – Port 8102)                   │
│                                                                  │
│  Separater Prozess für Echtzeit-Datenverteilung                 │
│  ├─ POST /api/broadcast  ← Empfängt Updates von Haupt-API      │
│  ├─ GET  /api/health     ← Health-Check                         │
│  ├─ GET  /api/status     ← Status-Abfrage                       │
│  ├─ WebSocket /ws        ← Client-Verbindungen                  │
│  └─ Kanal-Pub/Sub:                                               │
│     ├─ markers   (Kartenmarker-Updates)                          │
│     ├─ drawings  (Zeichnungs-Updates)                            │
│     ├─ overlays  (Overlay-Änderungen)                            │
│     ├─ messages  (Chat-Nachrichten)                              │
│     ├─ alerts    (Geofence/Regel-Alarme)                         │
│     ├─ positions (Positions-Updates)                              │
│     ├─ cot       (CoT-Events)                                    │
│     └─ camera    (Stream-Sharing)                                │
└─────────────────────────────────────────────────────────────────┘
```

---

## Port-Konfiguration

| Port | Dienst | Zweck |
|------|--------|-------|
| **8101** | Haupt-API-Server (api.py) | REST API + Statische Dateien + WebSocket-Fallback |
| **8102** | Daten-Server (data_server.py) | WebSocket /ws + HTTP /api/broadcast, /api/health, /api/status |

---

## Datenbank-Schema (SQLite – tactical.db)

| Tabelle | Zweck | Schlüsselfelder |
|---------|-------|-----------------|
| **users** | Benutzerkonten | id, username, password_hash, role, unit, device, callsign |
| **map_markers** | Taktische Kartenpunkte | id, name, lat, lng, type, color, icon, created_by, data (JSON) |
| **missions** | Missionsdefinitionen | id, name, description, status, data (JSON) |
| **meshtastic_nodes** | Mesh-Netzwerk-Geräte | id, long_name, short_name, lat, lng, hardware_model, raw_data |
| **chat_messages** | Kanal-basierte Nachrichten | id, channel, sender, content, timestamp |
| **drawings** | Kartenannotationen | id, name, type, coordinates (JSON), color, weight |
| **overlays** | Kartenüberlagerungen | id, name, image_url, bounds (JSON), opacity, rotation |
| **autonomous_rules** | Automatisierungsregeln | id, name, trigger_type, conditions, actions, enabled |
| **geofences** | Geographische Zonen | id, name, center_lat, center_lon, radius_meters, zone_type |
| **audit_log** | System-Audit-Trail | id, event_type, user, details, timestamp |
| **api_sessions** | Benutzersitzungen | id, token, user_id, username, expires_at |
| **user_groups** | Gruppenmitgliedschaften | id, name, description |
| **qr_codes** | QR-Code-Registrierungstoken | id, token, type, max_uses, expires_at |
| **pending_registrations** | Registrierungswarteschlange | id, token, username, password_hash |

---

## Python-Module

| Datei | Aufgabe |
|-------|---------|
| **api.py** | Haupt-FastAPI-Anwendung – ALLE REST-Endpunkte, Auth, CRUD |
| **database.py** | SQLAlchemy Engine & Session-Factory |
| **models.py** | SQLAlchemy ORM-Modelle für alle Daten-Entitäten |
| **websocket_manager.py** | WebSocket-Verbindungsverwaltung, Pub/Sub-Kanäle |
| **data_server.py** | Unabhängiger Datenverteilungsprozess (Port 8102) |
| **data_server_manager.py** | Startet & verwaltet data_server als Subprocess |
| **autonomous_engine.py** | Regelbasierte Automatisierung, Trigger, Aktionsausführung |
| **geofencing.py** | Zonenerstellung, Ein-/Austritts-Erkennung (Haversine) |
| **cot_protocol.py** | Cursor-on-Target XML Parsing/Generierung (ATAK/WINTAK) |
| **permissions_manager.py** | RBAC-System (derzeit DEAKTIVIERT – alle haben Vollzugriff) |
| **meshtastic_gateway_parser.py** | Parst Meshtastic-Gerätedaten |
| **migrate_db.py** | Datenbank-Schema-Initialisierung |
| **system_test.py** | System-Integrationstests |

---

## Prozess-Interaktionen & Datenfluss

### Beispiel: Benutzer erstellt Kartenmarker

1. Browser → `POST /api/map_markers` → Haupt-API (Port 8101)
2. Haupt-API speichert in SQLite-Datenbank
3. Haupt-API ruft `broadcast_websocket_update()` auf
4. Falls Daten-Server läuft:
   - `POST http://127.0.0.1:8102/api/broadcast` mit Marker-Daten
   - Daten-Server sendet an alle Clients im "markers"-Kanal via WebSocket
5. Falls Daten-Server nicht verfügbar (Fallback):
   - Direkter WebSocket-Broadcast via `websocket_manager.publish_to_channel()`
6. Verbundene Clients empfangen Echtzeit-Update

### Beispiel: Meshtastic-Node-Sync

1. `sync_meshtastic_nodes_to_map_markers_once()` wird periodisch aufgerufen
2. Liest alle MeshtasticNode-Einträge aus der Datenbank
3. Für jeden Node: Erstellt/Aktualisiert einen MapMarker mit `created_by="import_meshtastic"`
4. Broadcast an alle Clients

### Beispiel: Chat-Nachricht

1. Browser → `POST /api/chat/message` → Haupt-API
2. Nachricht wird in `chat_messages`-Tabelle gespeichert
3. Broadcast auf "messages"-Kanal
4. Alle verbundenen Clients im selben Channel erhalten die Nachricht

---

## Frontend-Dateien

| Datei | Funktion |
|-------|----------|
| **index.html** | Hauptüberwachungs-Dashboard (Einheiten-Tabelle, Status) |
| **admin.html** | Benutzer- & Gruppenverwaltung |
| **admin_map.html** | Kartenansicht mit taktischen Overlays |
| **mission.html** | Missionserstellung & -zuweisung |
| **network.html** | Meshtastic-Knotenverwaltung |
| **stream.html** | Kamera-Feed-Integration |
| **stream_share.html** | Geteilter Stream-Viewer |
| **meshtastic.html** | Mesh-Gerätedetails |
| **overview.html** | Systemübersicht/Statistiken |
| **statistics.html** | Historische Datenanalyse |
| **register.html** | Benutzerregistrierungsformular |
| **landing.html** | Öffentliche Landing-Page |
| **import_nodes.html** | Meshtastic-Import-Assistent |
| **language.html** | Sprachauswahl |
| **_global_nav.html** | Gemeinsame Navigationskomponente |

---

## Autonome Operationen

### Regel-Engine (autonomous_engine.py)
- **Trigger-Typen:** `geofence`, `time`, `status_change`, `message`, `manual`
- **Regeln:** Bedingungen (Logik) + Aktionen (Ausführung) + Prioritätsstufen
- **Aktionen:** Alarme senden, Benachrichtigungen auslösen, Befehle ausführen

### Geofencing (geofencing.py)
- Kreis-Zonen (Mittelpunkt + Radius in Metern)
- Zonentypen: `exclusion`, `inclusion`, `alert`, `safe`
- Ein-/Austritts-Erkennung mittels Haversine-Distanzberechnung

### CoT-Protokoll (cot_protocol.py)
- XML-basiertes Cursor-on-Target-Format
- ATAK/WINTAK-Kompatibilität
- Taktische Symbole und Zugehörigkeitscodes

---

## Erkannte & Behobene Fehler

### 🔴 KRITISCH

| # | Problem | Datei | Zeilen | Fix |
|---|---------|-------|--------|-----|
| 1 | **Port-Konflikt:** data_server.py startete auf Port 8101, gleich wie Haupt-API. data_server_manager (api.py) erwartete Port 8102. | data_server.py:40 | `DATA_SERVER_PORT = 8101` → `8102` | ✅ Behoben |
| 2 | **WebSocket-Log zeigte falschen Port** | api.py:782 | `ws://127.0.0.1:8101/ws` → `8102` | ✅ Behoben |
| 3 | **MeshtasticNode: nicht existierende Attribute** `mesh_id`, `name`, `callsign`, `device` verwendet statt `long_name`, `short_name`, `hardware_model` | api.py:650-670 | Attribute korrigiert | ✅ Behoben |
| 4 | **MapMarker.timestamp existiert nicht** – korrekt ist `created_at` | api.py:663 | `marker.timestamp` → `marker.created_at` | ✅ Behoben |
| 5 | **MapMarker.unit_id und .status existieren nicht** – müssen über `data` JSON-Feld abgefragt werden | api.py:1453-1467 | Filter und Zugriff über `data` JSON korrigiert | ✅ Behoben |

### 🟡 HOCH

| # | Problem | Datei | Zeilen | Fix |
|---|---------|-------|--------|-----|
| 6 | **Overlay.type existiert nicht** im Model, wird aber in Broadcast referenziert | api.py:733 | Entfernt aus Dictionary | ✅ Behoben |
| 7 | **Doppelte Routen:** `/api/sync/markers`, `/api/sync/overlays`, `/api/sync/drawings` doppelt definiert – zweite Version verwendet undefinierte Variable `manager` (statt `websocket_manager`) | api.py:4125-4275 | Duplikate entfernt | ✅ Behoben |
| 8 | **Bare except:** Fängt alle Exceptions inkl. KeyboardInterrupt | api.py:1145 | Spezifische Exception-Typen | ✅ Behoben |

### 🟡 MITTEL

| # | Problem | Datei | Zeilen | Fix |
|---|---------|-------|--------|-----|
| 9 | **tuple[] Type-Annotation:** `tuple[bool, Optional[str]]` erfordert Python 3.9+ | meshtastic_gateway_parser.py:87 | `Tuple[bool, Optional[str]]` mit typing-Import | ✅ Behoben |

---

## Sicherheit & Authentifizierung

- **JWT Token-basiert** (HS256, 24h Ablauf)
- **Passwort-Hashing:** SHA-256
- **Audit-Logging:** Alle Aktionen werden protokolliert
- **CORS:** `allow_origins=["*"]` (offen für alle Ursprünge)
- **Berechtigungssystem:** Derzeit DEAKTIVIERT – alle authentifizierten Benutzer haben Vollzugriff
- **SSL/HTTPS:** Optional via cert.pem/key.pem

---

## Konfiguration (config.json)

```json
{
    "tactical_overlay": null,
    "marker_broadcast_enabled": true,
    "marker_broadcast_interval_seconds": 60
}
```

---

## Wichtige API-Endpunkte

### Authentifizierung
- `POST /api/login_user` – Benutzer-Login
- `POST /api/register_user` – Registrierung
- `GET /api/me` – Aktueller Benutzer

### Taktische Daten
- `GET/POST /api/map_markers` – Marker CRUD
- `GET/POST /api/drawings` – Zeichnungen CRUD
- `GET/POST /api/overlays` – Overlays CRUD
- `GET/POST /api/symbols` – Symboldefinitionen

### Missionen
- `GET/POST /api/missions` – Missions CRUD
- `POST /api/mission_complete/{id}/{result}` – Mission abschließen

### Synchronisierung
- `POST /api/sync/markers` – Marker-Sync & Broadcast
- `POST /api/sync/overlays` – Overlay-Sync & Broadcast
- `POST /api/sync/drawings` – Zeichnungs-Sync & Broadcast
- `POST /api/sync/upload` – Unified Sync Upload

### Chat
- `GET /api/chat/channels` – Kanäle auflisten
- `POST /api/chat/message` – Nachricht senden
- `GET /api/chat/messages/{channel_id}` – Nachrichten abrufen

### Autonome Operationen
- `POST/GET /api/rules` – Regel CRUD
- `POST /api/rules/trigger` – Manueller Trigger
- `GET/POST /api/geofences` – Geofence CRUD
- `GET/POST /api/cot_events` – CoT-Events
