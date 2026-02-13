# Multi-Platform Architecture

## Overview

The LPU5 Tactical system has been architected to support two distinct platform implementations, each optimized for specific use cases and device capabilities.

## Platform Split Rationale

### Why Split iOS and Android?

1. **Browser API Limitations**
   - iOS Safari does not support Web Bluetooth API
   - Android Chrome has full Web Bluetooth support
   - Native Android allows direct hardware access

2. **Use Case Optimization**
   - iOS devices typically used at HQ with reliable internet
   - Android devices used in field requiring mesh communication
   - Different connectivity requirements justify different approaches

3. **Operational Roles**
   - **HQ Role**: Command center with internet, centralized coordination
   - **Field Role**: Mobile operators, mesh-only communication, GPS tracking

## Architecture Diagrams

### iOS PWA Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     iOS Device                          │
│  ┌───────────────────────────────────────────────────┐  │
│  │              Safari Browser                       │  │
│  │  ┌─────────────────────────────────────────────┐  │  │
│  │  │         LPU5 Tactical PWA                   │  │  │
│  │  │  - overview.html                            │  │  │
│  │  │  - Service Worker (offline cache)           │  │  │
│  │  │  - IndexedDB (local storage)                │  │  │
│  │  │  - Leaflet Map                              │  │  │
│  │  └─────────────────────────────────────────────┘  │  │
│  │                      ↕ HTTPS/WSS                  │  │
│  └──────────────────────┼────────────────────────────┘  │
└────────────────────────┼──────────────────────────────┘
                         │
                    Internet/LTE
                         │
┌────────────────────────┼──────────────────────────────┐
│                    HQ Server                          │
│  ┌──────────────────────────────────────────────────┐ │
│  │         FastAPI Backend (api.py)                 │ │
│  │  - REST API Endpoints                            │ │
│  │  - WebSocket Server                              │ │
│  │  - Authentication (JWT)                          │ │
│  │  - Database (SQLite)                             │ │
│  └───────────────────┬──────────────────────────────┘ │
│                      │                                 │
│  ┌──────────────────┴──────────────────────────────┐ │
│  │   Meshtastic Gateway Service (Optional)         │ │
│  │  - Serial Port Connection                       │ │
│  │  - Message Forwarding                           │ │
│  │  - Node Synchronization                         │ │
│  └───────────────────┬──────────────────────────────┘ │
└────────────────────┼────────────────────────────────┘
                     │ USB/Serial
                     ↓
              Meshtastic Device
                  (LoRa Mesh)
```

**Data Flow (iOS)**:
1. User interacts with PWA in Safari
2. PWA sends HTTPS requests to HQ server
3. Server processes and responds
4. WebSocket provides real-time updates
5. Optional: Server forwards to mesh via gateway
6. Service Worker caches for offline access

### Android Native Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  Android Device                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │         LPU5 Tactical Native App                  │  │
│  │  ┌─────────────────────────────────────────────┐  │  │
│  │  │  WebView (overview.html)                    │  │  │
│  │  │  - Map Display                              │  │  │
│  │  │  - UI Components                            │  │  │
│  │  │  - JavaScript Logic                         │  │  │
│  │  └────────────┬────────────────────────────────┘  │  │
│  │               ↕ JavaScript Bridge                 │  │
│  │  ┌────────────┴────────────────────────────────┐  │  │
│  │  │  Native Android Layer (Kotlin)              │  │  │
│  │  │  - MainActivity                             │  │  │
│  │  │  - Meshtastic SDK Integration               │  │  │
│  │  │  - GPS/Location Services                    │  │  │
│  │  │  - BLE Manager                              │  │  │
│  │  │  - Permission Handling                      │  │  │
│  │  └────────────┬────────────────────────────────┘  │  │
│  └───────────────┼────────────────────────────────────┘  │
│                  │ Bluetooth LE                          │
└──────────────────┼───────────────────────────────────────┘
                   │
                   ↓
        ┌──────────────────────┐
        │  Meshtastic Device   │
        │  - BLE Radio         │
        │  - LoRa Transceiver  │
        │  - GPS Module        │
        └──────────┬───────────┘
                   │
                   ↓ LoRa RF
            Mesh Network
```

**Data Flow (Android)**:
1. User interacts with WebView UI
2. JavaScript calls native Android functions via bridge
3. Native layer uses Meshtastic SDK
4. Direct BLE communication to Meshtastic device
5. Device transmits over LoRa mesh
6. GPS updates from native location services

## Component Architecture

### Shared Components

Both platforms share the web UI layer:

```
┌────────────────────────────────────────┐
│         overview.html (Web UI)         │
├────────────────────────────────────────┤
│  - Leaflet.js Map                      │
│  - Meshtastic Control Panel            │
│  - Chat Interface                      │
│  - COT Message Display                 │
│  - Marker Management                   │
└────────────────────────────────────────┘
          ↓ Uses
┌────────────────────────────────────────┐
│      JavaScript Client Libraries       │
├────────────────────────────────────────┤
│  meshtastic-web-client.js              │
│  - Web Bluetooth wrapper (Android)     │
│  - Packet encoding/decoding            │
│  - Event handling                      │
├────────────────────────────────────────┤
│  cot-client.js                         │
│  - COT XML generation                  │
│  - Message parsing                     │
│  - ATAK compatibility                  │
├────────────────────────────────────────┤
│  message-queue-manager.js              │
│  - IndexedDB queue                     │
│  - Retry logic                         │
│  - Offline persistence                 │
└────────────────────────────────────────┘
```

### Platform-Specific Components

#### iOS PWA Stack
```
overview.html (PWA version)
    ↓
manifest.json (iOS optimized)
    ↓
sw.js (Service Worker)
    ↓
Browser APIs (no Bluetooth)
    ↓
Fetch API → REST API
    ↓
HQ Server (api.py)
```

#### Android Native Stack
```
MainActivity.kt
    ↓
WebView (embedded overview.html)
    ↓
JavaScript Bridge
    ↓
Meshtastic SDK
    ↓
BLE Stack
    ↓
Hardware Device
```

## Communication Protocols

### iOS PWA ↔ HQ Server

**REST API Endpoints:**
```
POST   /api/login_user
GET    /api/map_markers
POST   /api/map_markers
PUT    /api/map_markers/{id}
DELETE /api/map_markers/{id}
GET    /api/meshtastic/nodes
POST   /api/meshtastic/send
GET    /api/gateway/status
POST   /api/gateway/send-message
```

**WebSocket Events:**
```javascript
{
  type: "marker_update",
  data: { id: 1, lat: 47.123, lon: 8.456 }
}

{
  type: "gateway_message",
  data: { from: "MESH-001", text: "Hello" }
}

{
  type: "gateway_node_update",
  data: { nodeId: "abc123", position: {...} }
}
```

### Android Native ↔ Meshtastic Device

**JavaScript Bridge Interface:**
```javascript
// WebView → Native
window.nativeConnectMeshtastic()
window.nativeSendMessage(message, isCOT)
window.nativeGetPosition()
window.nativeGetMeshtasticNodes()

// Native → WebView
window.onAndroidEvent(event, data)
```

**Meshtastic Protocol:**
- Uses official Meshtastic Android SDK
- Protobuf-based message encoding
- BLE GATT characteristics
- Automatic node discovery
- Packet routing and mesh topology

### COT Protocol (Both Platforms)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="MESH-USER-001" 
       type="a-f-G-U-C" how="m-g"
       time="2024-01-01T12:00:00Z" 
       start="2024-01-01T12:00:00Z" 
       stale="2024-01-01T12:05:00Z">
  <point lat="47.1234" lon="8.5678" 
         hae="500" ce="10" le="5"/>
  <detail>
    <contact callsign="Alpha-1"/>
    <remarks>Position update</remarks>
  </detail>
</event>
```

## Data Storage

### iOS PWA Storage
```
IndexedDB Databases:
  - pendingMessages (outgoing queue)
  - sentMessages (history)
  - receivedMessages (inbox)
  - nodes (mesh node cache)

Service Worker Cache:
  - HTML/CSS/JS files
  - Map tiles (limited)
  - Static assets
```

### Android Native Storage
```
SharedPreferences:
  - User settings
  - Connection state

SQLite Database:
  - Message history
  - Node database
  - Cached map data

Internal Storage:
  - Map tile cache
  - Downloaded overlays
  - Export files
```

## Security Model

### iOS PWA Security

**Transport Layer:**
- HTTPS/TLS 1.3 required
- Certificate validation
- Secure WebSocket (WSS)

**Authentication:**
```
Client → Server: POST /api/login_user
Server → Client: JWT token
Client → Server: Authorization: Bearer <token>
```

**Data Protection:**
- HTTPS encrypts all traffic
- JWT tokens expire (configurable)
- CORS restricts origins
- Rate limiting on endpoints

### Android Native Security

**App Signing:**
- APK signed with release key
- ProGuard obfuscation
- Verify app signature on install

**Permissions:**
```xml
Runtime permissions required:
- ACCESS_FINE_LOCATION
- BLUETOOTH_CONNECT
- BLUETOOTH_SCAN
```

**Meshtastic Encryption:**
- Channel-level AES-256 encryption
- Unique channel keys
- Admin channel separation

## Offline Capabilities

### iOS PWA Offline Mode

**Service Worker Strategy:**
```javascript
// Cache-first for static assets
self.addEventListener('fetch', event => {
  if (event.request.url.includes('/static/')) {
    event.respondWith(
      caches.match(event.request)
        .then(response => response || fetch(event.request))
    );
  }
});
```

**Limitations:**
- No new data without server connection
- Cached map tiles only
- No mesh communication
- UI remains functional
- Queued actions sync on reconnect

### Android Native Offline Mode

**Full Offline Capabilities:**
- Direct mesh communication (no internet)
- GPS position tracking
- Local message storage
- Offline map tiles
- Background operation
- Automatic mesh routing

**No Server Required:**
- 100% independent operation
- Mesh-to-mesh communication
- Local data persistence
- Battery-optimized background service

## Deployment Strategies

### iOS PWA Deployment

```
Development:
  Local HTTPS server → iOS device on same network

Staging:
  Cloud server → Test group via public IP

Production:
  HQ server → All iOS devices via domain/IP
```

**Update Process:**
1. Update files on server
2. Increment service worker version
3. Restart server
4. iOS devices auto-update on reconnect

### Android Native Deployment

```
Development:
  Android Studio → USB-connected device

Testing:
  Build APK → Manual distribution

Production:
  Signed APK → Google Play Store
  OR
  Enterprise distribution → MDM/EMM
```

**Update Process:**
1. Increment version in build.gradle
2. Build signed APK
3. Upload to distribution channel
4. Users install update

## Scalability Considerations

### iOS PWA (Server-Side)

**Concurrent Users:**
- FastAPI: ~1000 concurrent WebSocket connections
- Uvicorn: Multi-worker deployment
- Nginx: Reverse proxy + load balancing
- Database: PostgreSQL for production (vs SQLite)

**Horizontal Scaling:**
```
┌─────────┐
│ Nginx   │
│ Load    │
│ Balancer│
└────┬────┘
     ├─────→ [API Server 1] → [PostgreSQL]
     ├─────→ [API Server 2] → [PostgreSQL]
     └─────→ [API Server 3] → [PostgreSQL]
```

### Android Native (Client-Side)

**Per-Device Limits:**
- Meshtastic mesh: 100+ nodes
- BLE connections: 1 active device
- GPS updates: Configurable interval
- Message queue: Device storage limit

**Mesh Scaling:**
- LoRa range: 1-10km+ (terrain dependent)
- Mesh hops: Auto-routing up to 3-4 hops
- Network size: 100+ devices practical

## Performance Metrics

### iOS PWA
- **Initial Load**: ~2-3 seconds (cached: <500ms)
- **API Response**: <100ms local, <500ms remote
- **WebSocket Latency**: <50ms
- **Map Rendering**: 30-60 FPS
- **Memory Usage**: ~50-100MB

### Android Native
- **App Launch**: <1 second
- **BLE Connection**: 2-5 seconds
- **GPS Fix**: 5-30 seconds (cold start)
- **Message Send**: <1 second (LoRa: 1-30 seconds)
- **Memory Usage**: ~100-150MB
- **Battery Impact**: Moderate (BLE + GPS)

## Future Enhancements

### Planned Features

**iOS PWA:**
- [ ] Push notifications via server
- [ ] Enhanced offline map caching
- [ ] Voice message support
- [ ] File attachments
- [ ] Multi-language support

**Android Native:**
- [ ] Serial USB connection (OTG)
- [ ] Multiple simultaneous Meshtastic devices
- [ ] Background mesh relay mode
- [ ] Advanced route optimization
- [ ] Mesh network topology view
- [ ] Plugin system for extensions

**Both Platforms:**
- [ ] End-to-end encryption
- [ ] Group chat channels
- [ ] Mission planning tools
- [ ] Waypoint navigation
- [ ] AR marker overlay
- [ ] Integration with other TAK platforms

## Conclusion

This multi-platform architecture provides:
- **Optimal user experience** for each platform
- **Maximum capabilities** within technical constraints
- **Flexible deployment** options
- **Scalable infrastructure** for growth
- **Secure communication** channels
- **Offline-first** design philosophy

The split approach ensures iOS users get a robust web-based solution while Android users benefit from native mesh integration, creating a comprehensive tactical communication system.
