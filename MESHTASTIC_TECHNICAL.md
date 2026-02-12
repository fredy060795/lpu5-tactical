# Meshtastic PWA - Technical Implementation

## Architecture Overview

The Meshtastic PWA integration follows a layered architecture designed for complete offline operation:

```
┌─────────────────────────────────────────────────────┐
│              User Interface (overview.html)          │
│  - Meshtastic Panel                                  │
│  - Map Integration (Leaflet)                         │
│  - Chat Window                                       │
└─────────────────────────────────────────────────────┘
                        ↕
┌─────────────────────────────────────────────────────┐
│           Application Layer (JavaScript)             │
│  - Event Handlers                                    │
│  - UI Updates                                        │
│  - Map Marker Management                             │
└─────────────────────────────────────────────────────┘
                        ↕
┌─────────────────────────────────────────────────────┐
│              Core Libraries                          │
│  ┌────────────────────────────────────────────────┐ │
│  │  meshtastic-web-client.js                      │ │
│  │  - Web Bluetooth API wrapper                   │ │
│  │  - Packet encoding/decoding                    │ │
│  │  - Event callbacks                             │ │
│  └────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────┐ │
│  │  cot-client.js                                 │ │
│  │  - COT XML generation                          │ │
│  │  - COT XML parsing                             │ │
│  │  - Marker conversion                           │ │
│  └────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────┐ │
│  │  message-queue-manager.js                      │ │
│  │  - IndexedDB operations                        │ │
│  │  - Message queuing                             │ │
│  │  - Retry logic                                 │ │
│  └────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
                        ↕
┌─────────────────────────────────────────────────────┐
│              Browser APIs                            │
│  - Web Bluetooth API (Device Communication)         │
│  - IndexedDB (Offline Storage)                      │
│  - Service Worker (Offline Caching)                 │
│  - Geolocation API (Position)                       │
└─────────────────────────────────────────────────────┘
                        ↕
┌─────────────────────────────────────────────────────┐
│         Hardware/System Layer                        │
│  - Bluetooth Radio                                   │
│  - Meshtastic Device (BLE → LoRa)                   │
│  - Mesh Network                                      │
└─────────────────────────────────────────────────────┘
```

## Component Details

### 1. meshtastic-web-client.js

#### Purpose
Provides a high-level JavaScript interface to Meshtastic devices via Web Bluetooth API.

#### Key Classes
- **MeshtasticWebClient**: Main client class

#### Key Methods
```javascript
// Connection
async connect() - Request and connect to device
async disconnect() - Disconnect from device
isSupported() - Check Web Bluetooth support

// Messaging
async sendText(text, channelIndex) - Send text message
async sendCOT(cotXml, channelIndex) - Send COT message
async sendPosition(lat, lon, alt) - Send position update

// Data Access
getNodes() - Get discovered mesh nodes
getMessages() - Get message history

// Callbacks
onMessage(callback) - Register message handler
onNodeUpdate(callback) - Register node update handler
onStatus(callback) - Register status change handler
```

#### Packet Format
Simplified packet structure (real implementation would use Meshtastic protobuf):

```
Header (12 bytes):
  - to (4 bytes): Destination node ID (0xFFFFFFFF for broadcast)
  - from (4 bytes): Source node ID
  - type (1 byte): Packet type (0x01=text, 0x02=position, 0x03=nodeinfo)
  - reserved (3 bytes)

Payload (variable):
  - Text: UTF-8 encoded string
  - Position: lat(4), lon(4), alt(4) as float32
  - NodeInfo: name(32 bytes), role, etc.
```

#### Connection Flow
```
1. User clicks "Connect Device"
2. navigator.bluetooth.requestDevice() shows picker
3. User selects Meshtastic device
4. gatt.connect() establishes connection
5. getPrimaryService(MESHTASTIC_SERVICE_UUID)
6. Get characteristics (toRadio, fromRadio, fromNum)
7. Start notifications on fromRadio
8. Register characteristicvaluechanged handler
9. Send config request
10. Connection established
```

### 2. cot-client.js

#### Purpose
Handles COT (Cursor on Target) protocol for ATAK/WinTAK compatibility.

#### Key Classes

##### COTEvent
Represents a single COT event with all required and optional fields.

```javascript
constructor(options) {
  uid: string,          // Unique identifier
  type: string,         // COT type (e.g., "a-f-G-U-C")
  lat: float,           // Latitude
  lon: float,           // Longitude
  hae: float,           // Height above ellipsoid
  ce: float,            // Circular error
  le: float,            // Linear error
  callsign: string,     // Display name
  remarks: string,      // Additional info
  teamName: string,     // Team/group name
  teamRole: string,     // Role in team
  how: string,          // How generated (m-g = machine)
  time: Date,           // Event time
  start: Date,          // Start time
  stale: Date           // Expiry time
}
```

##### COTProtocolHandler
Utility class for COT operations.

```javascript
static markerToCOT(marker) - Convert map marker to COT
static cotToMarker(cotEvent) - Convert COT to map marker
static validateCOTXML(xml) - Validate COT XML structure
static isCOTMessage(text) - Check if text is COT XML
```

#### COT Type Structure
```
a-f-G-U-C
│ │ │ │ │
│ │ │ │ └─ Detail: C=Combat, I=Intelligence, etc.
│ │ │ └─── Function: U=Unit, E=Equipment, etc.
│ │ └───── Entity: G=Ground, A=Air, S=Sea, etc.
│ └─────── Affiliation: f=Friendly, h=Hostile, n=Neutral, u=Unknown
└───────── Atom: a=Atom (all COT events start with 'a')
```

#### Example COT XML
```xml
<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="MESH-001" type="a-f-G-U-C" 
       how="m-g" time="2024-01-01T12:00:00Z" 
       start="2024-01-01T12:00:00Z" 
       stale="2024-01-01T12:05:00Z">
  <point lat="47.1234" lon="8.5678" hae="500" ce="10" le="5"/>
  <detail>
    <contact callsign="Alpha-1"/>
    <__group name="Team Alpha" role="Lead"/>
    <remarks>Position update</remarks>
    <track speed="0.0" course="0.0"/>
  </detail>
</event>
```

### 3. message-queue-manager.js

#### Purpose
Manages offline message queue using IndexedDB for persistence.

#### Database Schema

```javascript
Database: MeshtasticOfflineDB (version 1)

ObjectStore: pendingMessages
  - keyPath: id (auto-increment)
  - Indexes: timestamp, status
  - Fields:
    * id: number
    * text: string
    * type: 'text' | 'cot'
    * timestamp: number
    * status: 'pending' | 'failed'
    * retryCount: number
    * maxRetries: number
    * lastRetryTimestamp: number

ObjectStore: sentMessages
  - keyPath: id (auto-increment)
  - Index: timestamp
  - Fields:
    * id: number
    * text: string
    * type: 'text' | 'cot'
    * timestamp: number
    * sentTimestamp: number

ObjectStore: receivedMessages
  - keyPath: id (auto-increment)
  - Indexes: timestamp, from
  - Fields:
    * id: number
    * from: string
    * text: string
    * type: 'text' | 'cot'
    * timestamp: number
    * read: boolean
    * isCOT: boolean

ObjectStore: nodes
  - keyPath: id
  - Index: timestamp
  - Fields:
    * id: number (Meshtastic node ID)
    * name: string
    * role: string
    * lat: number
    * lon: number
    * alt: number
    * timestamp: number
```

#### Message Lifecycle

```
1. User composes message
2. addPendingMessage() → IndexedDB
3. If connected: attempt send via Bluetooth
4. On success: markAsSent() → moves to sentMessages
5. On failure: incrementRetry() → retry count++
6. If retryCount < maxRetries: retry after 30s
7. If retryCount >= maxRetries: status = 'failed'
```

#### Retry Logic
```javascript
processPendingMessages() {
  for each pending message:
    if status !== 'failed':
      try send:
        on success: markAsSent()
        on failure: incrementRetry()
          if retryCount >= 3: mark as failed
}

// Called every 30 seconds when connected
```

### 4. Service Worker (sw.js)

#### Purpose
Enables offline functionality by caching all required assets.

#### Cache Strategy
```javascript
CACHE_NAME: 'lpu5-v2-meshtastic'

Cached Assets:
  - HTML pages (overview.html, landing.html, etc.)
  - JavaScript libraries (meshtastic-web-client.js, cot-client.js, etc.)
  - CSS frameworks (Leaflet, Font Awesome)
  - App manifest and icons

Strategy:
  1. Cache First: Check cache before network
  2. Network Fallback: If not in cache, fetch from network
  3. Update Cache: Store successful network responses
  4. Offline Fallback: Show offline page if both fail
```

#### Cache Flow
```
Request → SW Intercept → Cache Check
                              ↓
                         Found in Cache?
                         ↙         ↘
                      YES          NO
                       ↓            ↓
                  Return       Fetch from Network
                   Cached           ↓
                   Response    Success?
                              ↙       ↘
                            YES       NO
                             ↓         ↓
                        Update Cache  Return
                        Return New    Offline
                        Response      Page
```

### 5. PWA Manifest (manifest.json)

#### Configuration
```json
{
  "name": "LPU5 Tactical Network with Meshtastic",
  "short_name": "LPU5-Mesh",
  "start_url": "/overview.html",
  "display": "standalone",
  "theme_color": "#28a745",
  "icons": [...],
  "categories": ["navigation", "utilities"],
  "features": [
    "Web Bluetooth API",
    "Offline First",
    "Meshtastic LoRa",
    "COT Protocol"
  ]
}
```

## Data Flow

### Sending a Message

```
User Input → UI Handler
              ↓
       Validate & Format
              ↓
    Queue in IndexedDB ←─────┐
              ↓               │
       Connected?             │
         ↙      ↘             │
       YES      NO            │
        ↓        ↓            │
   Send via → Queue for      │
   Bluetooth   Later Send    │
        ↓                     │
   Success? ───────────────→ │
     ↙   ↘                    │
   YES   NO                   │
    ↓     ↓                   │
  Mark  Retry ────────────────┘
  Sent   Logic
```

### Receiving a Message

```
Meshtastic Device → BLE Notification
                          ↓
              fromRadio characteristic
                          ↓
            characteristicvaluechanged Event
                          ↓
              Parse Packet (MeshtasticWebClient)
                          ↓
                   Packet Type?
                  ↙      ↓      ↘
              Text   Position  NodeInfo
                ↓       ↓         ↓
            onMessage  onNode   onNode
            Callback  Callback  Callback
                ↓       ↓         ↓
         Store in   Update     Update
         IndexedDB  Node DB    Node DB
                ↓       ↓         ↓
           Display   Add to    Update
           in Chat    Map      Node List
                ↓
          Is COT? ───→ Parse COT
                          ↓
                    Add Marker to Map
```

### Map Marker Integration

```
COT Message Received
        ↓
  Parse COT XML
        ↓
 Extract Coordinates
        ↓
 Create Leaflet Marker
        ↓
   Set Icon Style
   (green for COT)
        ↓
   Add to Map Layer
        ↓
 Bind Popup with Info
```

## Security Considerations

### Bluetooth Security
- Pairing required before first connection
- BLE encryption (device-dependent)
- User must explicitly authorize connection
- Cannot access Bluetooth without user interaction

### Message Security
- No built-in encryption in this implementation
- Rely on Meshtastic device encryption settings
- Messages transmitted over LoRa as configured on device
- Consider sensitive data carefully

### Storage Security
- IndexedDB is origin-isolated (per domain)
- No cross-origin access
- Browser's security model applies
- Data persists until manually cleared

### Network Security
- Service Worker requires HTTPS (or localhost)
- All cached resources use HTTPS
- No sensitive credentials stored

## Performance Optimization

### Bluetooth Communication
- Chunked packet transmission (512 bytes max)
- Notification-based reception (no polling)
- Async operations throughout
- Error handling with retry logic

### Database Operations
- Indexed queries for fast lookups
- Batch operations where possible
- Automatic cleanup of old messages (7 days)
- Efficient cursor-based iteration

### UI Updates
- Debouncing for rapid events
- Virtual scrolling for large lists (future)
- Lazy loading of markers
- RequestAnimationFrame for smooth animations

### Caching Strategy
- Aggressive caching for static assets
- Network-first for dynamic content
- Preload critical resources
- Lazy load non-critical resources

## Testing Recommendations

### Unit Testing
- COT XML generation/parsing
- Message queue operations
- Packet encoding/decoding
- Marker conversion

### Integration Testing
- Bluetooth connection flow
- Message send/receive cycle
- Offline queue processing
- Map marker synchronization

### Manual Testing
1. **Connection**: Test pairing with various devices
2. **Messaging**: Send/receive text and COT
3. **Offline**: Disconnect and queue messages
4. **Recovery**: Reconnect and verify queued messages send
5. **Map**: Verify markers appear correctly
6. **Export**: Test data export functionality

### Device Testing
- Android phones (Chrome)
- Windows PCs (Chrome/Edge)
- Chromebooks
- Different Meshtastic hardware (T-Beam, Heltec, etc.)

## Future Enhancements

### Possible Improvements
1. **Protocol Buffers**: Use proper Meshtastic protobuf encoding
2. **Import Data**: Support importing backup files
3. **Message Filtering**: Filter by node, type, time range
4. **Custom Channels**: UI for channel selection
5. **Device Settings**: Configure Meshtastic device settings
6. **Telemetry**: Display battery, signal strength, etc.
7. **Route Planning**: Integrate with navigation
8. **Group Messaging**: Private message groups
9. **File Transfer**: Send images/files via mesh
10. **Encryption UI**: Manage encryption keys

### Known Limitations
1. **iOS Support**: Limited Web Bluetooth on iOS
2. **Simplified Protocol**: Not full Meshtastic protocol implementation
3. **Single Connection**: One device at a time
4. **No Background Sync**: Requires app open to receive
5. **Basic Error Recovery**: Could be more robust

## Troubleshooting Development Issues

### Bluetooth Connection Fails
```javascript
// Check browser support
if (!navigator.bluetooth) {
  console.error('Web Bluetooth not supported');
}

// Check for HTTPS
if (location.protocol !== 'https:' && location.hostname !== 'localhost') {
  console.error('Web Bluetooth requires HTTPS');
}

// Enable Chrome flags (if needed)
// chrome://flags/#enable-web-bluetooth-new-permissions-backend
```

### IndexedDB Errors
```javascript
// Check for private/incognito mode
if (!window.indexedDB) {
  console.error('IndexedDB not available (private mode?)');
}

// Handle quota exceeded
db.onerror = (event) => {
  if (event.target.error.name === 'QuotaExceededError') {
    // Clear old data or request more storage
  }
};
```

### Service Worker Issues
```javascript
// Force update service worker
navigator.serviceWorker.getRegistration().then(reg => {
  reg.update();
});

// Clear caches
caches.keys().then(keys => {
  keys.forEach(key => caches.delete(key));
});
```

## Development Setup

### Running Locally
```bash
# Option 1: Python simple server with HTTPS
python -m http.server 8000 --bind localhost

# Option 2: Use localhost (Bluetooth works without HTTPS on localhost)
# Simply open overview.html in Chrome from file:// or local server

# Option 3: Use a proper HTTPS server
# Install mkcert for local certificates
mkcert localhost 127.0.0.1 ::1
python -m http.server 8000 --bind localhost # with SSL wrapper
```

### Debugging
```javascript
// Enable verbose logging
localStorage.setItem('mesh-debug', 'true');

// View IndexedDB in Chrome DevTools
// Application → Storage → IndexedDB → MeshtasticOfflineDB

// Monitor Bluetooth
// Chrome → chrome://bluetooth-internals

// Service Worker debugging
// Chrome → Application → Service Workers → inspect
```

## References

- [Web Bluetooth API Specification](https://webbluetoothcg.github.io/web-bluetooth/)
- [Meshtastic Bluetooth Protocol](https://meshtastic.org/docs/development/bluetooth/)
- [COT Protocol Specification](https://www.mitre.org/sites/default/files/pdf/09_4937.pdf)
- [IndexedDB API](https://developer.mozilla.org/en-US/docs/Web/API/IndexedDB_API)
- [Service Worker API](https://developer.mozilla.org/en-US/docs/Web/API/Service_Worker_API)
- [PWA Best Practices](https://web.dev/progressive-web-apps/)

---

*Document Version: 1.0*  
*Last Updated: 2024*  
*Author: LPU5 Tactical Development Team*
