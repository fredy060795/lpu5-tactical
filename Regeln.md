# Regeln für Agents

> **Pflichtlektüre vor jedem Pull Request**

---

## 1. Pflichtlektüre

Der Agent **muss** diese Regeln vor jedem Pull Request lesen und sicherstellen, dass alle Änderungen den nachfolgenden Vorgaben entsprechen.

## 2. Verbot von Simulationen

Es ist **verboten**, Simulationen in das System einzubauen.
Alle Implementierungen müssen auf echten Daten und realen Systemkomponenten basieren. Simulierte Zustände, Mock-Daten oder Dummy-Implementierungen dürfen nicht in den produktiven Code einfließen.

## 3. Hardware-Abhängigkeiten prüfen und kommunizieren

Wenn eine Funktion Hardware-Abhängigkeiten benötigt (z. B. `rtl_tcp` für SDR, serielle Schnittstellen für Meshtastic), **muss** das System:

1. **Beim Start prüfen**, ob die benötigten System-Tools und Python-Pakete installiert sind.
2. **Den Benutzer klar informieren**, wenn eine Abhängigkeit fehlt – mit dem genauen Namen und dem Installationsbefehl.
3. **Automatisch installieren**, was per `pip install` möglich ist (Python-Pakete werden über `requirements.txt` beim Start installiert).
4. **Auf fehlende System-Tools hinweisen**, die manuell installiert werden müssen (z. B. `sudo apt install rtl-sdr`), inklusive dem konkreten Installationsbefehl.
5. **Den API-Endpunkt `/api/dependencies/check`** aktuell halten, sodass zur Laufzeit der vollständige Status aller Abhängigkeiten abgefragt werden kann.

### Beispiele für Hardware-Abhängigkeiten

| Abhängigkeit | Typ | Funktion | Installationsbefehl |
|---|---|---|---|
| `rtl_tcp` | System-Tool | SDR TCP-Server | `sudo apt install rtl-sdr` |
| `rtl_power` | System-Tool | SDR Frequenzscan | `sudo apt install rtl-sdr` |
| `rtl_fm` | System-Tool | SDR FM-Demodulation | `sudo apt install rtl-sdr` |
| `pyrtlsdr` | Python-Paket | Direkte SDR-Hardware | `pip install pyrtlsdr` |
| `numpy` | Python-Paket | Schnelle FFT-Verarbeitung | `pip install numpy` |
| `pyserial` | Python-Paket | Serielle Ports | `pip install pyserial` |
| `meshtastic` | Python-Paket | Meshtastic-Geräte | `pip install meshtastic` |

---

## ✅ Bestätigung durch den Agent

**Ja**, der Agent wird diese Regeln in Zukunft befolgen.

- Vor jedem Pull Request werden diese Regeln gelesen und geprüft.
- Es werden keine Simulationen, Mock-Daten oder Dummy-Implementierungen in den produktiven Code eingefügt.
- Fehlende Hardware-Abhängigkeiten werden beim Start geprüft, kommuniziert und – soweit möglich – automatisch installiert.

_Diese Bestätigung gilt für alle zukünftigen Aktionen des Agents in diesem Repository._
