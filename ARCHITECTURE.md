# Meshtastic PWA Integration - Visual Summary

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     USER INTERFACE (overview.html)               │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐   │
│  │  Leaflet Map │  │  Chat Window │  │ Meshtastic Panel   │   │
│  │  - Markers   │  │  - Messages  │  │ - Connection UI    │   │
│  │  - Nodes     │  │  - COT msgs  │  │ - Message Compose  │   │
│  │  - COT viz   │  │  - History   │  │ - Node List        │   │
│  └──────────────┘  └──────────────┘  └────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              ↕ JavaScript Events
┌─────────────────────────────────────────────────────────────────┐
│              APPLICATION LOGIC (Inline JavaScript)               │
│  - Event Handlers                                                │
│  - UI Updates                                                    │
│  - Map Marker Management                                         │
│  - Message Display                                               │
└─────────────────────────────────────────────────────────────────┘
                              ↕ API Calls
┌─────────────────────────────────────────────────────────────────┐
│                   CORE LIBRARIES (3 JavaScript Files)            │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  meshtastic-web-client.js (11.7 KB)                        │ │
│  │  - Web Bluetooth API Wrapper                               │ │
│  │  - Device Connection Management                            │ │
│  │  - Packet Encoding/Decoding                                │ │
│  │  - Event Callbacks (onMessage, onNodeUpdate, onStatus)     │ │
│  │  - Methods: connect(), sendText(), sendCOT(), getNodes()   │ │
│  └────────────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  cot-client.js (12.2 KB)                                   │ │
│  │  - COTEvent Class                                          │ │
│  │  - COT XML Generation                                      │ │
│  │  - COT XML Parsing (DOMParser)                            │ │
│  │  - COTProtocolHandler Utilities                           │ │
│  │  - Marker ↔ COT Conversion                                │ │
│  └────────────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  message-queue-manager.js (13.2 KB)                        │ │
│  │  - IndexedDB Wrapper                                       │ │
│  │  - Message Queue (pending, sent, received)                 │ │
│  │  - Node Storage                                            │ │
│  │  - Retry Logic (max 3 attempts)                           │ │
│  │  - Export/Import Functionality                            │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                              ↕ Browser APIs
┌─────────────────────────────────────────────────────────────────┐
│                    BROWSER APIs (Native)                         │
│  ┌──────────────┐  ┌─────────────┐  ┌──────────────────────┐  │
│  │ Web Bluetooth│  │  IndexedDB  │  │   Service Worker     │  │
│  │     API      │  │             │  │   (sw.js - v2)       │  │
│  │ - requestDev │  │ - Messages  │  │ - Asset Caching      │  │
│  │ - GATT conn  │  │ - Nodes     │  │ - Offline Mode       │  │
│  │ - BLE notify │  │ - Queue     │  │ - Cache First        │  │
│  └──────────────┘  └─────────────┘  └──────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ↕
┌─────────────────────────────────────────────────────────────────┐
│              HARDWARE LAYER (Physical Devices)                   │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                   Meshtastic Device                       │  │
│  │  ┌──────────┐      ┌──────────┐      ┌──────────────┐  │  │
│  │  │Bluetooth │ ←──→ │  Radio   │ ←──→ │ Mesh Network │  │  │
│  │  │   (BLE)  │      │  (LoRa)  │      │   (Others)   │  │  │
│  │  └──────────┘      └──────────┘      └──────────────┘  │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Data Flow - Sending a Message

```
User Types Message
       ↓
   Click "Send"
       ↓
sendMeshtasticMessage()
       ↓
  COT checkbox?
   ↙        ↘
 YES        NO
  ↓          ↓
Generate   Use Plain
COT XML    Text
  ↓          ↓
  └─────┬────┘
        ↓
messageQueue.addPendingMessage()
        ↓
    IndexedDB
    (persists)
        ↓
   Connected?
   ↙        ↘
 YES        NO
  ↓          ↓
Send via   Queue for
Bluetooth  Later
  ↓          ↓
  │      (retry in
  │       30 sec)
  ↓
meshtasticClient.sendText/COT()
  ↓
Web Bluetooth API
  ↓
toRadio characteristic
  ↓
Meshtastic Device
  ↓
LoRa Transmission
  ↓
Mesh Network
```

## Data Flow - Receiving a Message

```
Mesh Network
    ↓
LoRa Reception
    ↓
Meshtastic Device
    ↓
fromRadio characteristic
    ↓
BLE Notification
    ↓
characteristicvaluechanged
    ↓
_handleIncomingData()
    ↓
_parsePacket()
    ↓
Packet Type?
↙    ↓    ↘
Text Pos  Node
 ↓    ↓    ↓
     Callbacks
        ↓
handleIncomingMessage()
        ↓
messageQueue.addReceivedMessage()
        ↓
    IndexedDB
        ↓
displayMessageInChat()
        ↓
  Chat Window
        ↓
   Is COT?
   ↙    ↘
 YES    NO
  ↓     (done)
Parse COT XML
  ↓
Extract Coords
  ↓
Create Marker
  ↓
Add to Map
```

## Message Queue State Machine

```
┌─────────────┐
│   COMPOSE   │
│   MESSAGE   │
└──────┬──────┘
       ↓
┌──────▼──────┐
│   PENDING   │◄────────┐
│   (Queue)   │         │
└──────┬──────┘         │
       ↓                │
   Connected?           │
    ↙    ↘              │
  YES     NO            │
   ↓      ↓             │
 Send   Wait            │
   ↓                    │
Success?                │
 ↙    ↘                 │
YES    NO               │
 ↓     ↓                │
 │  Increment           │
 │  Retry Count         │
 │     ↓                │
 │  < 3 retries? ───────┘
 │     ↓
 │    NO
 │     ↓
 │  ┌──▼──────┐
 │  │ FAILED  │
 │  └─────────┘
 ↓
┌▼────────┐
│  SENT   │
└─────────┘
```

## File Structure

```
lpu5-tactical/
│
├── overview.html (175 KB)           ← Main PWA with Meshtastic UI
│   ├── HTML Structure
│   │   ├── Leaflet Map Container
│   │   ├── Chat Window
│   │   └── Meshtastic Panel (NEW)
│   ├── CSS Styles
│   │   ├── Existing tactical styles
│   │   └── Meshtastic panel styles (NEW)
│   └── JavaScript
│       ├── Existing map logic
│       └── Meshtastic integration (NEW)
│
├── meshtastic-web-client.js (11.7 KB)   ← Bluetooth Client
│   └── MeshtasticWebClient Class
│       ├── connect() / disconnect()
│       ├── sendText() / sendCOT()
│       ├── getNodes() / getMessages()
│       └── Event callbacks
│
├── cot-client.js (12.2 KB)              ← COT Protocol
│   ├── COTEvent Class
│   │   ├── toXML()
│   │   ├── fromXML()
│   │   ├── toDict()
│   │   └── buildCOTType()
│   └── COTProtocolHandler Class
│       ├── markerToCOT()
│       ├── cotToMarker()
│       └── validateCOTXML()
│
├── message-queue-manager.js (13.2 KB)   ← Offline Queue
│   └── MessageQueueManager Class
│       ├── IndexedDB Operations
│       ├── addPendingMessage()
│       ├── markAsSent()
│       ├── incrementRetry()
│       ├── getStats()
│       └── exportData()
│
├── sw.js (2.9 KB)                       ← Service Worker v2
│   ├── Asset caching
│   ├── Offline fallback
│   └── Cache management
│
├── manifest.json (0.6 KB)               ← PWA Manifest
│   ├── App metadata
│   ├── Icons
│   └── Display settings
│
├── MESHTASTIC_GUIDE.md (9.4 KB)         ← User Documentation
├── MESHTASTIC_TECHNICAL.md (17.3 KB)    ← Technical Docs
├── test-meshtastic-integration.js (9.0 KB)  ← Tests
└── README.md (updated)                  ← Project Overview
```

## Technology Stack

```
┌─────────────────────────────────────────────────┐
│                  Frontend                        │
│  - HTML5 (semantic, accessibility)              │
│  - CSS3 (responsive, mobile-first)              │
│  - Vanilla JavaScript (ES6+, async/await)       │
│  - Leaflet.js (mapping)                         │
│  - Font Awesome (icons)                         │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│               Browser APIs                       │
│  - Web Bluetooth API (device communication)     │
│  - IndexedDB (offline storage)                  │
│  - Service Workers (offline caching)            │
│  - DOMParser (XML parsing)                      │
│  - Geolocation API (user position)              │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│              Protocols                           │
│  - Meshtastic Protocol (over BLE)               │
│  - COT Protocol v2.0 (ATAK/WinTAK)              │
│  - LoRa (physical layer - device handles)       │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│           No Dependencies On                     │
│  ✗ Node.js / npm                                │
│  ✗ Build tools (webpack, etc.)                  │
│  ✗ Backend server                               │
│  ✗ Internet connection                          │
│  ✗ External APIs                                │
└─────────────────────────────────────────────────┘
```

## Performance Characteristics

```
┌──────────────────────────────────────────────────┐
│              Metrics                              │
├──────────────────────────────────────────────────┤
│ Total Bundle Size:        ~212 KB                │
│ Initial Load Time:        < 2 seconds            │
│ Service Worker Cache:     ~10 MB (configurable)  │
│ IndexedDB Storage:        ~50 MB (browser limit) │
│ Bluetooth Latency:        10-100 ms              │
│ LoRa Message Latency:     1-30 seconds           │
│ Battery Impact:           Low (BLE LE)           │
│ Offline Capable:          100%                   │
└──────────────────────────────────────────────────┘
```

## Security Model

```
┌─────────────────────────────────────────────────┐
│           Security Layers                        │
├─────────────────────────────────────────────────┤
│ Transport:        HTTPS required (or localhost)  │
│ Pairing:          User must authorize BLE       │
│ Storage:          Origin-isolated IndexedDB     │
│ Cache:            Service Worker same-origin    │
│ Encryption:       Meshtastic device handles     │
│ Input Validation: XML parsing with error check  │
│ XSS Prevention:   DOM escaping for user input   │
└─────────────────────────────────────────────────┘
```

## Deployment Options

```
Option 1: Static Web Server
  └─ Copy files to any web server
     - Apache, Nginx, IIS, etc.
     - GitHub Pages, Netlify, Vercel
     - S3 + CloudFront, etc.
     - Requires HTTPS

Option 2: Local File System
  └─ Open overview.html directly
     - file:// protocol
     - Some features may be limited
     - Use for development

Option 3: Python Simple Server
  └─ python -m http.server 8000
     - localhost automatically allowed
     - Good for testing

Option 4: Docker Container
  └─ nginx:alpine + static files
     - Portable deployment
     - Easy scaling
```

## Browser Feature Detection

```javascript
// Pseudo-code for feature checks

if (!navigator.bluetooth) {
    // Web Bluetooth not supported
    // Show error message
    // Fallback: use backend gateway
}

if (!window.indexedDB) {
    // IndexedDB not available
    // Possibly in private/incognito mode
    // Fallback: use localStorage (limited)
}

if (!('serviceWorker' in navigator)) {
    // Service Worker not supported
    // App will work but not offline
    // Show warning
}

// Check for HTTPS
if (location.protocol !== 'https:' && 
    location.hostname !== 'localhost') {
    // Web Bluetooth requires HTTPS
    // Show error
}
```

## Development Workflow

```
1. Edit Code
   ├─ overview.html (UI changes)
   ├─ meshtastic-web-client.js (BLE logic)
   ├─ cot-client.js (COT protocol)
   └─ message-queue-manager.js (storage)

2. Test Locally
   ├─ Open in Chrome/Edge
   ├─ Use Chrome DevTools
   │  ├─ Application → Service Workers
   │  ├─ Application → IndexedDB
   │  └─ More Tools → Bluetooth Internals
   └─ Mock Meshtastic device (optional)

3. Run Automated Tests
   └─ node test-meshtastic-integration.js

4. Test with Real Hardware
   ├─ Pair actual Meshtastic device
   ├─ Send test messages
   ├─ Verify COT parsing
   └─ Check offline queue

5. Deploy
   └─ Copy files to web server
```

## Troubleshooting Quick Reference

```
Problem: "Web Bluetooth not supported"
Solution: Use Chrome/Edge/Opera on Android/Windows

Problem: "Connection failed"
Solution: Check Bluetooth enabled, device powered on

Problem: Messages not sending
Solution: Verify connection (green dot), check queue

Problem: Can't install PWA
Solution: Ensure HTTPS, check manifest.json

Problem: No nodes appearing
Solution: Wait for broadcasts (15-30 min intervals)

Problem: COT messages not parsing
Solution: Validate XML format, check logs

Problem: IndexedDB errors
Solution: Check storage quota, clear old data

Problem: Service Worker not updating
Solution: Hard refresh (Ctrl+Shift+R), clear cache
```

---

**Document Version:** 1.0  
**Last Updated:** December 2024  
**Status:** Production Ready  
**Implementation:** Complete
