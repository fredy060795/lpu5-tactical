# LPU5 Tactical – Ionic / Capacitor

Dieses Verzeichnis enthält das **Ionic/Capacitor**-Projekt für LPU5 Tactical.
Mit einem einzigen Codebase lassen sich sowohl ein **Android APK** als auch eine
**iOS App** erstellen – kein separates natives Android-Studio-Projekt und kein
separates PWA-Setup mehr nötig.

---

## Voraussetzungen

| Tool | Mindestversion | Zweck |
|------|---------------|-------|
| [Node.js](https://nodejs.org/) | 18 LTS | npm / Capacitor CLI |
| [Android Studio](https://developer.android.com/studio) | Electric Eel | Android APK bauen |
| [Xcode](https://developer.apple.com/xcode/) | 14 | iOS App bauen (**nur macOS**) |
| [CocoaPods](https://cocoapods.org/) | 1.12 | iOS Dependencies (**nur macOS**) |

---

## Schnellstart

```bash
# 1. In dieses Verzeichnis wechseln
cd lpu5-tactical/ionic

# 2. Abhängigkeiten installieren
npm install

# 3a. Android-Plattform hinzufügen (einmalig)
npm run add:android

# 3b. iOS-Plattform hinzufügen (einmalig, nur macOS)
npm run add:ios

# 4. Web-Assets und Plugins synchronisieren (nach jeder Änderung)
npm run sync
```

---

## Android APK erstellen

### Schritt 1 – Projekt synchronisieren

```bash
npm run sync
```

### Schritt 2 – In Android Studio öffnen

```bash
npm run open:android
```

Android Studio öffnet automatisch das generierte Projekt unter `android/`.

### Schritt 3 – Debug-APK bauen

In Android Studio:
- Menü → **Build → Build Bundle(s) / APK(s) → Build APK(s)**
- APK liegt unter: `android/app/build/outputs/apk/debug/app-debug.apk`

Oder über die Kommandozeile im `android/`-Ordner:

```bash
cd android
./gradlew assembleDebug
# Ausgabe: app/build/outputs/apk/debug/app-debug.apk
```

### Schritt 4 – Release-APK (mit Signierung)

```bash
# Keystore erstellen (einmalig)
keytool -genkey -v -keystore lpu5-tactical.keystore \
  -alias lpu5-key -keyalg RSA -keysize 2048 -validity 10000

# Release bauen
cd android
./gradlew assembleRelease
```

> ⚠️ Keystore-Passwörter **niemals** in die Versionskontrolle einchecken.
> Nutze `~/.gradle/gradle.properties` oder Umgebungsvariablen.

### APK direkt auf Gerät installieren

```bash
adb install android/app/build/outputs/apk/debug/app-debug.apk
```

---

## iOS App erstellen (nur macOS)

### Schritt 1 – Projekt synchronisieren

```bash
npm run sync
```

### Schritt 2 – In Xcode öffnen

```bash
npm run open:ios
```

Xcode öffnet automatisch das generierte Projekt unter `ios/App/App.xcworkspace`.

### Schritt 3 – Signierung konfigurieren

In Xcode:
1. Projekt-Navigator → **App** auswählen
2. Tab **Signing & Capabilities**
3. Team und Bundle Identifier eintragen (`com.lpu5.tactical`)

### Schritt 4 – App bauen und starten

- Gerät oder Simulator auswählen → **▶ Run** drücken
- Für Archiv (App Store / TestFlight): Menü → **Product → Archive**

---

## Workflow bei Code-Änderungen

```
┌─────────────────────────────────────────────┐
│  Änderungen in ionic/www/*.html / *.js      │
└──────────────┬──────────────────────────────┘
               │
               ▼
        npm run sync        ← kopiert www/ in android/ und ios/
               │
       ┌───────┴───────┐
       ▼               ▼
  Android Studio     Xcode
  (APK bauen)      (iOS bauen)
```

---

## Projektstruktur

```
ionic/
├── capacitor.config.json   # Capacitor-Konfiguration (App-ID, webDir, Plugins)
├── package.json            # npm-Abhängigkeiten + Build-Skripte
├── README.md               # Diese Datei
├── www/                    # Web-Assets (werden in native Projekte kopiert)
│   ├── index.html          # Haupt-HTML (map UI + Login)
│   ├── capacitor-bridge.js # Native Bridge: GPS, BLE, Toast via Capacitor
│   ├── meshtastic-web-client.js  # Meshtastic BLE Protokoll-Client
│   ├── cot-client.js       # COT-Protokoll Handler
│   ├── message-queue-manager.js  # Offline-Nachrichten-Queue
│   ├── manifest.json       # PWA-Manifest
│   └── logo.png            # App-Icon
├── android/                # Generiertes Android-Projekt (von cap add android)
└── ios/                    # Generiertes iOS-Projekt (von cap add ios)
```

> Die Ordner `android/` und `ios/` werden **automatisch** von Capacitor generiert.
> Sie sind nicht manuell zu bearbeiten – alle Änderungen gehen in `www/`.

---

## Native Funktionen (capacitor-bridge.js)

Die Datei `www/capacitor-bridge.js` stellt folgende native Funktionen bereit,
die automatisch aktiv sind wenn die App auf Android **oder iOS** läuft:

| Funktion | Plugin | Beschreibung |
|----------|--------|--------------|
| `window.nativeGetPosition()` | `@capacitor/geolocation` | Aktuelle GPS-Position (JSON) |
| `window.nativeConnectMeshtastic()` | `@capacitor-community/bluetooth-le` | BLE Gerätesuche & Verbindung (Android + iOS) |
| `window.nativeDisconnectMeshtastic()` | `@capacitor-community/bluetooth-le` | BLE Gerät trennen |
| `window.nativeSendMessage(msg, isCOT)` | `@capacitor-community/bluetooth-le` | Nachricht via Mesh senden |
| `window.nativeGetMeshtasticNodes()` | `@capacitor-community/bluetooth-le` | Verbundene Nodes (JSON) |
| `window.Android.showToast(msg)` | `@capacitor/toast` | Nativer Toast-Hinweis |

**Plattform-Erkennung:**

| Flag | Beschreibung |
|------|-------------|
| `window.isCapacitorNative` | `true` wenn App auf Android oder iOS läuft |
| `window.isIOSNative` | `true` wenn App auf iOS läuft |
| `window.isAndroidNative` | `true` wenn App auf Android läuft |
| `window.hasNativeMeshtastic` | `true` auf beiden Plattformen (BLE verfügbar) |

Im Desktop-Browser werden diese Funktionen automatisch deaktiviert – der Code
fällt dann auf die Web-Bluetooth-API zurück.

### Events (window.onAndroidEvent)

```javascript
window.onAndroidEvent = function(event, data) {
    const payload = JSON.parse(data);
    switch (event) {
        case 'locationUpdate':        // { latitude, longitude, altitude, accuracy }
        case 'meshtasticMessage':     // { raw: base64 }
        case 'meshtasticServiceConnected':    // { status, device }
        case 'meshtasticServiceDisconnected': // { status, message }
        case 'messageSent':           // { message, isCOT }
    }
};
```

---

## Berechtigungen

Die folgenden Berechtigungen werden von Capacitor automatisch in die nativen
Projekte eingetragen:

### Android (`android/app/src/main/AndroidManifest.xml`)
- `ACCESS_FINE_LOCATION` / `ACCESS_COARSE_LOCATION`
- `BLUETOOTH` / `BLUETOOTH_ADMIN` / `BLUETOOTH_CONNECT` / `BLUETOOTH_SCAN`
- `INTERNET`
- `CAMERA`

### iOS (`ios/App/App/Info.plist`)
- `NSLocationWhenInUseUsageDescription`
- `NSBluetoothAlwaysUsageDescription`
- `NSCameraUsageDescription`

---

## Fehlerbehebung

### `npx cap sync` schlägt fehl
```bash
npm install          # Abhängigkeiten neu installieren
npx cap sync --inline
```

### Gradle-Fehler in Android Studio
```bash
cd android
./gradlew clean
./gradlew build
```

### Xcode: "No provisioning profile found"
- Apple Developer Account in Xcode unter Preferences → Accounts eintragen
- Bundle Identifier muss eindeutig sein (z.B. `com.DEINNAME.lpu5tactical`)

### Geolocation / BLE funktioniert nicht
- Berechtigungen auf dem Gerät manuell unter Einstellungen prüfen
- Android: Standort-Dienste (High Accuracy) aktivieren
- iOS: Datenschutz → Ortungsdienste aktivieren

---

## Vergleich: Vorher vs. Nachher

| | Vorher (getrennt) | Jetzt (Ionic/Capacitor) |
|-|-------------------|------------------------|
| Android | natives Kotlin/Android Studio Projekt in `android/` | `npm run open:android` → Android Studio |
| iOS | nur PWA (kein echter nativer Wrapper) | `npm run open:ios` → Xcode |
| Codebase | 2 getrennte Projekte | **1 gemeinsame `www/` Codebase** |
| GPS | native Kotlin API | `@capacitor/geolocation` |
| BLE | native Kotlin API | `@capacitor-community/bluetooth-le` |
| Updates | assets manuell kopieren | `npm run sync` |

---

## Weiterführende Links

- [Capacitor Docs](https://capacitorjs.com/docs)
- [Capacitor Android Guide](https://capacitorjs.com/docs/android)
- [Capacitor iOS Guide](https://capacitorjs.com/docs/ios)
- [@capacitor-community/bluetooth-le](https://github.com/capacitor-community/bluetooth-le)
- [@capacitor/geolocation](https://capacitorjs.com/docs/apis/geolocation)
