# Funktzions-Übersicht

Dies ist eine übersicht aller Funktzionen, alle funktzionen die ein # vor dem Namen der Funktzion besitzen dürfen nicht mehr ohne ausdrückliche anweisung Bearbeitet werden, da sie Perfekt Funktzionieren!

---

## overview.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | GPS Position Laden bei Start | Eigene GPS-Position wird beim Seitenstart automatisch geladen und auf der Karte zentriert |
| [#] | Batterieanzeige im Header | Akkustand des Geräts wird im Kopfbereich angezeigt |
| [#] | Uhrzeit im Header | Aktuelle Uhrzeit wird live im Kopfbereich dargestellt |
| [#] | Kompass Funktion / Karten drehen | Karte dreht sich automatisch anhand des Geräte-Kompass |
| [#] | Karten Layout (Sat / Street / Geo) im Menü | Umschalten zwischen Satelliten-, Straßen- und Geländekarte |
| [#] | Echtzeit WebSocket Chat | Mehrkanaliger Live-Chat über WebSocket-Verbindung |
| [#] | WebRTC Medien-Streaming | Einbindung von Live-Video-Streams über WebRTC |
| [#] | Missions-Tracking mit Statusanzeige | Laufende Missionen werden mit Farbstatus auf der Karte angezeigt |
| [#] | Marker-Clustering & Zoom-Rendering | Symbole werden je nach Zoomstufe gebündelt oder einzeln dargestellt |
| [#] | Chat-Deduplizierung | Verhindert doppelte Nachrichten im Chat |
| [#] | Serververbindung prüfen / Heartbeat | Verbindungsstatus zum Server wird laufend überwacht |
| [#] | Fadenkreuz-Positionierungs-Tool | Exakte Koordinatenauswahl per Fadenkreuz auf der Karte |

---

## admin.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | Multi-Tab-Dashboard | Verwaltungsoberfläche mit Tabs (Nutzer, Geräte, Missionen usw.) |
| [#] | Benutzerverwaltung (CRUD) | Anlegen, Lesen, Bearbeiten und Löschen von Benutzern |
| [#] | Geräteverwaltung & Zuweisung | Geräte anlegen und Nutzern zuweisen |
| [#] | Missions-Lifecycle-Verwaltung | Missionen erstellen, bearbeiten und abschließen |
| [#] | Login / Logout | Authentifizierung mit Token-Verwaltung |
| [#] | Benutzername im Header anzeigen | Angemeldeter Nutzername wird im Kopfbereich eingeblendet |

---

## admin_map.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | Einheitliche Admin-Kartenoberfläche | Kombinierte Admin- und Kartenansicht für alle taktischen Funktionen |
| [#] | Symbolebenen-Verwaltung | Einheiten-Symbole auf der Karte verwalten und positionieren |
| [#] | Fadenkreuz zur Koordinatenauswahl | Exakte Positionsauswahl für Admin-Aktionen |
| [#] | Eigene Position zentrieren | Karte auf eigene GPS-Position zentrieren |
| [#] | Geplante Karten-Updates | Automatische Kartenaktualisierung im Intervall |
| [#] | Sidebar-Navigation (einklappbar) | Seitenleiste mit aus-/einklappbarem Navigationsmenü |

---

## mission.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | Missions-Erstellungs-Formular | Militärisches Auftragsformular (5-Punkte-Befehl) |
| [#] | Gesamtbefehl / Operationsbefehl | Vollständiger Operationsauftrag als Freitextfeld |
| [#] | Lagebeurteilung (Feind / Eigene / Beistellungen) | Feind- und Eigenkräfte sowie Unterstellungen eingeben |
| [#] | Auftragsdefinition | Klarer Missionsauftrag mit Ziel und Zweck |
| [#] | Durchführungsplanung | Vorgehensweise, Plan und Koordination |
| [#] | Dienst & Versorgung | Verpflegung, Pausenzeiten und Logistik |
| [#] | Führung & Verbindungen (C2) | Kanäle, Gruppen und Führungsort festlegen |

---

## mission_Overview.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | Missions-Seitenleiste mit Filter | Liste aller Missionen, abgeschlossene ein-/ausblendbar |
| [#] | Missions-Detailansicht | Vollständige Missionsdetails auf Auswahl anzeigen |
| [#] | Anhang-Viewer (Bilder / PDF) | Bilder als Vorschau und PDFs eingebettet anzeigen |
| [#] | Farbcodierter Status | ONGOING=gelb, SUCCESS=grün, FAILED=rot |
| [#] | Meta-Leiste (Datum / AO / Start / Ende) | Metadaten der Mission kompakt im Überblick |

---

## network.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | Netzwerkkonfiguration | Server-IP, Port und TAK-Host-Einstellungen speichern |
| [#] | Serverstatus laden | Verbindungs- und Statusinfo vom Server abrufen |
| [] | TAK Host-Einstellungen | TAK-Serveradresse konfigurieren |
| [#] | IP-Validierung | Eingabe-Prüfung für IP-Adressen |
| [#] | Einstellungen speichern/laden (API) | Netzwerkeinstellungen über API persistieren |

---

## statistics.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | Missions-Archiv & Statistiken | Historische Missionen und Leistungsdaten anzeigen |
| [#] | Einheiten-Performance-Tracking | Aktivitäten und Ergebnisse je Einheit auswerten |
| [#] | Statusdiagramme | Grafische Darstellung der Missions-Ergebnisse |
| [#] | Klickbare Detailansicht | Zeile anklicken für vollständige Missionsdetails |
| [#] | Status-Pillen (SUCCESS / FAILED / ONGOING) | Farbige Status-Kennzeichnung in Tabellen |

---

## meshtastic.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | Meshtastic Chat-Interface | Mehrkanaliger Chat über Meshtastic-Netzwerk |
| [#] | WebSocket Echtzeit-Messaging | Live-Nachrichten über WebSocket |
| [#] | Chat-Deduplizierung | Keine doppelten Nachrichten (Sender/Text/Zeitstempel) |
| [#] | Kanal-Liste | Alle verfügbaren Meshtastic-Kanäle auflisten |
| [#] | Nachrichten-History | Vorherige Nachrichten beim Öffnen laden |
| [#] | COT-Integration (Cursor on Target) | Positionsdaten über COT-Protokoll senden/empfangen |

---

## stream.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | Multi-Slot Stream-Viewer | Mehrere Video-Streams gleichzeitig in konfigurierbarem Raster |
| [#] | Quellauswahl (RTMP / HTTP / HLS) | Stream-Quelltyp und URL konfigurieren |
| [#] | Einheiten-Dropdown für Stream-Ziel | Einheit für Stream-Slot auswählen |
| [#] | Stream-Freigabe Steuerung | Streams freigeben und teilen |
| [#] | Nächsten freien Slot berechnen | Automatisch den nächsten verfügbaren Stream-Slot ermitteln |

---

## SDR.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | SDR Radar-Visualisierung | Frequenzanalyse und Radar-Darstellung via RTL-SDR |
| [#] | RTL-TCP Konfiguration | Host/Port für RTL-TCP-Server einstellen |
| [#] | Audio-Stream Integration | Audiostrom vom SDR-Gerät empfangen und abspielen |
| [#] | Treiber-Warnungssystem | Warnung bei fehlenden SDR-Bibliotheken |

---

## global_Intel.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | Globales Nachrichten-Dashboard | Klassifiziertes Informations-Dashboard mit Multi-Tile-Ansicht |
| [#] | Klassifizierungs-Banner | Taktische Geheimhaltungsstufe im Header anzeigen |
| [#] | Flug-Routen-Projektion | Flugrouten mit Kurs und Geschwindigkeit einblenden |
| [#] | Flugzeugdaten-Integration | ICAO, Herkunft, Flughöhe und Status anzeigen |
| [#] | Share-Buttons für Stream-Daten | Daten-Streams per Knopfdruck weitergeben |

---

## index.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | Taktischer Einheiten-Status-Tracker | Übersichtstabelle aller Einheiten mit Status und Timer |
| [#] | Gerätezuweisung via Dropdown | Gerät direkt in der Tabelle auswählen |
| [#] | Status-Pillen (ACTIVE / BASE / KIA / INACTIVE) | Farbige Echtzeit-Status je Einheit |
| [#] | WebSocket Echtzeit-Daten | Live-Update der Einheitendaten via WebSocket |
| [#] | Missions-Status-Anzeige | Aktuell laufende Mission im Überblick |
| [#] | Lokaler Status-Cache (localStorage) | Einheitenstatus lokal zwischenspeichern |

---

## landing.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | Login mit Token-Verwaltung | Authentifizierung und Token-Speicherung |
| [#] | Logout Funktion | Abmelden und Token löschen |
| [#] | API-Status prüfen | Verbindung zum Backend beim Start testen |
| [#] | App ein-/ausblenden nach Login-Status | UI je nach Authentifizierungsstatus anzeigen |

---

## language.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | Sprachauswahl (34+ Sprachen) | Systemsprache aus einer Übersicht auswählen |
| [#] | Sprachsuche / Filter | Sprachen per Suchfeld filtern |
| [#] | Aktive Sprache hervorheben | Aktuell gewählte Sprache wird markiert |
| [#] | Sprachpräferenz speichern | Auswahl im Nutzerprofil und SessionStorage speichern |

---

## register.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [#] | Nutzer-Registrierung mit QR-Code | Neues Konto über Formular oder QR-Code-Payload anlegen |
| [#] | QR Payload-Extraktion (Base64) | QR-Code-Inhalt dekodieren und Formular befüllen |
| [#] | Passwort-Validierung | Passwörter müssen übereinstimmen und mind. 6 Zeichen lang sein |
| [#] | Einheiten-Dropdown laden (API) | Verfügbare Einheiten vom Server abrufen |
| [#] | Offline-Fallback (localStorage) | Registrierung lokal speichern bei Serverausfall |

---

## import_nodes.html

| Status | Funktion | Kurzbeschreibung |
|--------|----------|-----------------|
| [] | Meshtastic-Node-Import | Gateway-Knoten importieren und konfigurieren |
| [#] | Port-Eingabe & Normalisierung | Serielle Ports erkennen und normalisieren |
| [#] | Toast-Benachrichtigungen | Nutzer-Feedback über Toast-Nachrichten |
| [#] | Node-JSON Import von Gateways | Knoten-Daten als JSON vom Gateway laden |
| [#] | Gerätekonnektivitäts-Test | Verbindung zu angeschlossenem Gerät prüfen |

---

> **Legende:**
> - `[#]` = Funktion ist perfekt und darf **nicht ohne ausdrückliche Anweisung** bearbeitet werden
> - `[]` = Funktion kann bearbeitet / weiterentwickelt werden
