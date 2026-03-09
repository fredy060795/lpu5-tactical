#⚠️ Project Status: Work in Progress
# LPU-5-system

## LPU5 Tactical Tracker

A comprehensive tactical tracking system with integrated Meshtastic support for real-time mesh network communication, COT (Cursor on Target) protocol, and position tracking. **Now split into multi-platform implementations**: iOS Progressive Web App (PWA) for HQ access and Android Native App for direct mesh communication.

## 🚀 Multi-Platform Architecture

This system provides **two optimized implementations**:

### 📱 iOS PWA (Progressive Web App)
- **Use Case**: HQ personnel, remote coordination
- **Access**: Via HQ's public IP using Safari
- **Communication**: REST API + WebSocket to HQ server
- **Limitations**: No direct Bluetooth (Safari restriction)
- **Deployment**: Add to Home Screen from web browser
- **Quick Start**: [pwa/IOS_INSTALL.md](pwa/IOS_INSTALL.md) - Step-by-step iOS installation
- **Technical Docs**: [pwa/README.md](pwa/README.md) - Detailed PWA documentation

### 🤖 Android Native App
- **Use Case**: Field operators, direct mesh communication
- **Access**: Native APK with embedded WebView
- **Communication**: Native BLE/Serial to Meshtastic devices
- **Features**: Full offline mesh, GPS tracking, COT exchange
- **Deployment**: Install APK or distribute via Play Store
- **Distribution Guide**: [android/DISTRIBUTION.md](android/DISTRIBUTION.md) - APK building and deployment
- **Technical Docs**: [android/README.md](android/README.md) - Detailed Android documentation

## Features

### Core Features (Both Platforms)
- **Real-time Map Display**: Live tactical map with markers, drawings, and overlays
- **User Management**: Role-based access control with authentication
- **Mission Planning**: Create and manage tactical missions
- **WebSocket Support**: Real-time updates across all connected clients
- **QR Code System**: Secure access via QR codes with expiration and usage limits
- **COT Protocol**: Full ATAK/WinTAK compatibility

### iOS PWA Features
- **Remote HQ Access**: Connect via HTTPS to HQ server
- **WebSocket Updates**: Real-time marker and message updates
- **Offline Caching**: Service Worker for offline map viewing
- **COT Display**: View COT messages from mesh network (via gateway)
- **Installable**: Add to home screen for native feel
- **Gateway Integration**: Receive mesh updates through HQ gateway
- **See**: [pwa/README.md](pwa/README.md) for iOS-specific guide

### Android Native App Features
- **Native Meshtastic SDK**: Direct BLE/Serial connection to mesh devices
- **Full Offline Operation**: 100% offline mesh networking
- **Native GPS**: High-accuracy position tracking
- **WebView Integration**: Embedded web UI with native bridge
- **COT Exchange**: Send and receive COT messages directly
- **Background Service**: Mesh connection persists in background
- **JavaScript Bridge**: Seamless web ↔ native communication
- **See**: [android/README.md](android/README.md) for Android-specific guide

### Meshtastic Gateway Service (Backend)
The integrated Meshtastic Gateway Service enables real-time hardware connection to Meshtastic devices for automatic data import and live tracking.

#### Gateway Features:
- **Hardware Connection**: Direct serial port connection to Meshtastic devices
- **Auto-Sync**: Automatic synchronization of nodes and messages
- **Real-time Updates**: Live WebSocket broadcasts for position updates
- **Message Handling**: Send and receive messages through the gateway
- **Node Tracking**: Automatic import of discovered Meshtastic nodes with GPS data
- **iOS Integration**: Enables iOS PWA to access mesh network via HQ

#### Gateway API Endpoints:
- `POST /api/gateway/start` - Start gateway service on specified port
- `POST /api/gateway/stop` - Stop gateway service
- `GET /api/gateway/status` - Get current status and statistics
- `POST /api/gateway/sync` - Trigger manual synchronization
- `GET /api/gateway/ports` - List available COM/serial ports
- `POST /api/gateway/test-port` - Test connection to a port
- `GET /api/gateway/nodes` - Get imported nodes
- `GET /api/gateway/messages` - Get received messages
- `POST /api/gateway/send-message` - Send message via gateway

#### Using the Gateway Service

**Via API:**
```bash
# Start gateway on COM7 (Windows) or /dev/ttyUSB0 (Linux)
curl -X POST http://localhost:8101/api/gateway/start \
  -H "Content-Type: application/json" \
  -d '{"port": "COM7", "auto_sync": true, "sync_interval": 300}'

# Check status
curl http://localhost:8101/api/gateway/status

# Stop gateway
curl -X POST http://localhost:8101/api/gateway/stop
```

**Via UI:**
1. Navigate to `/meshtastic.html` in your browser
2. Click "Scan Ports" to detect available serial ports
3. Select your Meshtastic device port
4. Click "Connect" to start the gateway
5. Monitor live nodes, messages, and logs in the UI panels

**Standalone:**
```bash
# Run gateway service independently
python meshtastic_gateway_service.py --port COM7 --auto-sync --sync-interval 300

# List available ports
python meshtastic_gateway_service.py --list-ports
```

## Quick Start

### For iOS Users (PWA)
1. **Server must be running at HQ** (see Installation below)
2. On iOS device, open Safari
3. Navigate to `https://your-hq-ip:8101/pwa/overview.html`
4. Tap Share → Add to Home Screen
5. Launch app from home screen
6. Login and start using

**See**: [pwa/README.md](pwa/README.md) for detailed iOS installation

### For Android Users (Native App)
1. **Build APK** (see [android/README.md](android/README.md))
2. Install APK on Android device
3. Grant Bluetooth and Location permissions
4. Connect to Meshtastic device via BLE
5. Start sending/receiving mesh messages

**See**: [android/README.md](android/README.md) for detailed Android setup

### For Backend/HQ Server
```bash
# Install dependencies
pip install -r requirements.txt

# Run the API server
uvicorn api:app --host 0.0.0.0 --port 8101 --ssl-keyfile key.pem --ssl-certfile cert.pem

# Optional: Start Meshtastic Gateway (for iOS PWA mesh access)
curl -X POST http://localhost:8101/api/gateway/start \
  -H "Content-Type: application/json" \
  -d '{"port": "COM7", "auto_sync": true, "sync_interval": 300}'
```

**See**: [DEPLOYMENT.md](DEPLOYMENT.md) for complete deployment guide

## Installation

### Requirements
- Python 3.8+
- FastAPI
- uvicorn
- SQLAlchemy
- pyserial (for Meshtastic gateway)
- meshtastic (for Meshtastic gateway)

### Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Run the server
uvicorn api:app --host 0.0.0.0 --port 8101 --ssl-keyfile key.pem --ssl-certfile cert.pem

# Or use the startup scripts:
# Linux/Unix/macOS:
./start_lpu5.sh

# Windows:
start_lpu5.bat
```

### Restarting the System

After making configuration changes or code updates, restart the server:

```bash
# Linux/Unix/macOS:
./restart_lpu5.sh

# Windows:
restart_lpu5.bat
```

See [RESTART_GUIDE.md](RESTART_GUIDE.md) for detailed restart procedures and troubleshooting.

### Optional: Meshtastic Gateway Dependencies
For gateway functionality, install additional packages:
```bash
pip install meshtastic>=2.7.7 pyserial>=3.5
```

The system works without these packages, but gateway features will be unavailable.

## Configuration

### Database
The system uses SQLite by default (`tactical.db`) with SQLAlchemy ORM. JSON files are used for legacy compatibility and gateway data storage.

### SSL Certificates
For HTTPS, place `key.pem` and `cert.pem` in the project root, or generate self-signed certificates:
```bash
openssl req -x509 -newkey rsa:4096 -nodes -keyout key.pem -out cert.pem -days 365
```

### Gateway Configuration
Gateway data is stored in:
- `meshtastic_nodes_db.json` - Imported nodes with GPS data
- `meshtastic_messages_db.json` - Received messages (max 1000)

## Architecture

### Components
1. **API Server** (`api.py`): FastAPI-based REST API
2. **Gateway Service** (`meshtastic_gateway_service.py`): Standalone Meshtastic hardware interface
3. **WebSocket Manager** (`websocket_manager.py`): Real-time communication
4. **Database Layer** (`database.py`, `models.py`): SQLAlchemy ORM
5. **Frontend**: HTML/JavaScript SPA with real-time updates

### Gateway Architecture
The gateway service runs in a separate thread managed by the API server. It:
1. Connects to Meshtastic hardware via serial port
2. Subscribes to packet events using pubsub
3. Processes incoming nodes and messages
4. Stores data in JSON database files
5. Broadcasts updates via WebSocket callbacks
6. Runs periodic sync operations in background thread

## Security

### Authentication
- JWT-based authentication with configurable expiration
- Bearer token in Authorization header
- Session management with database storage

### Gateway Security
- No external network exposure - local serial port only
- Graceful fallback when dependencies unavailable
- Thread-safe operations with proper locking
- Clean shutdown handling

## API Documentation

### Authentication
```
POST /api/login_user - Login with username/password
POST /api/register_user - Register new user
GET /api/me - Get current user info
```

### Map & Markers
```
GET /api/map_markers - Get all markers
POST /api/map_markers - Create marker
PUT /api/map_markers/{id} - Update marker
DELETE /api/map_markers/{id} - Delete marker
```

### Meshtastic (Legacy)
```
GET /api/meshtastic/nodes - Get nodes
POST /api/meshtastic/connect - Connect to device
POST /api/meshtastic/disconnect - Disconnect
POST /api/meshtastic/send - Send message
```

### Gateway (New)
See "Gateway API Endpoints" section above.

## WebSocket Events

Connect to `/ws` for real-time updates:

### Event Types:
- `marker_update` - Map marker changes
- `drawing_update` - Drawing updates
- `gateway_status` - Gateway connection status
- `gateway_node_update` - Node position update
- `gateway_message` - Incoming/outgoing message
- `gateway_log` - Gateway activity log

### Example:
```javascript
const ws = new WebSocket('wss://localhost:8101/ws');
ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === 'gateway_message') {
        console.log('New message:', data.text);
    }
};
```

## Troubleshooting

### Gateway Issues

**"Gateway service not available"**
- Install meshtastic and pyserial: `pip install meshtastic pyserial`

**"Failed to connect to device"**
- Check COM port is correct (use `/api/gateway/ports` to list)
- Ensure no other application is using the port
- Verify Meshtastic device is connected and powered on

**"Port operation lock timeout"**
- Another operation is in progress, wait and try again
- Stop and restart gateway if stuck

### Performance
- Gateway auto-sync default: 300 seconds (configurable)
- Message buffer: 1000 messages max
- Status polling: 5 seconds interval in UI
- PWA: IndexedDB for offline storage, Service Worker caching

## Platform Compatibility

### iOS PWA ✅
- **iOS 11.3+** with Safari
- Add to Home Screen for best experience
- HTTPS required
- **Note**: No direct Bluetooth (Safari limitation)
- Uses HQ server via REST API

### Android Native App ✅
- **Android 7.0+** (API 24+)
- Direct BLE via Meshtastic SDK
- Full offline mesh networking
- Native GPS integration

### Legacy Web (overview.html) ⚠️
For development or non-mobile access:
- **Chrome/Edge/Opera**: Full Web Bluetooth
- **Firefox**: No Web Bluetooth
- **Safari (macOS)**: Limited Bluetooth

**Recommendation**: Use platform-specific implementations (iOS PWA or Android Native) for production.

For detailed compatibility, see [DEPLOYMENT.md](DEPLOYMENT.md).

## Development

### Project Structure
```
lpu5-tactical/
├── pwa/                             # iOS Progressive Web App
│   ├── overview.html                # PWA main UI
│   ├── manifest.json                # iOS-optimized manifest
│   ├── sw.js                        # Service Worker
│   ├── *.js                         # Client libraries
│   └── README.md                    # iOS PWA guide
│
├── android/                         # Android Native Application
│   ├── app/                         # Android app module
│   │   ├── build.gradle             # App build config
│   │   └── src/main/
│   │       ├── AndroidManifest.xml  # Permissions & config
│   │       ├── java/.../MainActivity.kt  # Main activity
│   │       ├── res/                 # Resources
│   │       └── assets/www/          # Embedded web UI
│   ├── build.gradle                 # Project build config
│   └── README.md                    # Android app guide
│
├── api.py                           # Main API server
├── meshtastic_gateway_service.py    # Backend gateway service
├── websocket_manager.py             # WebSocket handler
├── database.py                      # Database connection
├── models.py                        # SQLAlchemy models
├── meshtastic.html                  # Backend gateway UI
├── overview.html                    # Original/legacy web UI
├── meshtastic-web-client.js         # Web Bluetooth client
├── cot-client.js                    # COT protocol implementation
├── message-queue-manager.js         # Offline message queue
│
├── DEPLOYMENT.md                    # Multi-platform deployment guide
├── MULTI_PLATFORM_ARCHITECTURE.md   # Architecture documentation
├── MESHTASTIC_GUIDE.md              # Meshtastic user guide
├── MESHTASTIC_TECHNICAL.md          # Technical documentation
├── index.html                       # Main map UI
├── requirements.txt                 # Python dependencies
└── README.md                        # This file
```

### Adding Features
1. API endpoints: Add to `api.py`
2. Database models: Add to `models.py`
3. Gateway features: Modify `meshtastic_gateway_service.py`
4. iOS PWA: Update files in `pwa/` directory
5. Android Native: Modify files in `android/` directory

## Documentation

### Platform-Specific Guides
- **[iOS PWA Guide](pwa/README.md)** - Complete iOS installation and usage
- **[Android Native Guide](android/README.md)** - Android app development and deployment
- **[Deployment Guide](DEPLOYMENT.md)** - Detailed deployment for both platforms
- **[Architecture Guide](MULTI_PLATFORM_ARCHITECTURE.md)** - System architecture and design
- **[Restart Guide](RESTART_GUIDE.md)** - System restart procedures and troubleshooting

### Technical Documentation
- **[Meshtastic User Guide](MESHTASTIC_GUIDE.md)** - Using Meshtastic features
- **[Meshtastic Technical](MESHTASTIC_TECHNICAL.md)** - Technical implementation details

### Quick Reference
| Document | Purpose | Audience |
|----------|---------|----------|
| [README.md](README.md) | Project overview | Everyone |
| [PLATFORM_COMPARISON.md](PLATFORM_COMPARISON.md) | **Platform selection guide** | **Decision makers** |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Installation & deployment | Administrators |
| [pwa/IOS_INSTALL.md](pwa/IOS_INSTALL.md) | **iOS installation walkthrough** | **iOS users** |
| [pwa/README.md](pwa/README.md) | iOS PWA technical docs | iOS users & developers |
| [android/DISTRIBUTION.md](android/DISTRIBUTION.md) | **Android APK distribution** | **IT staff** |
| [android/README.md](android/README.md) | Android app technical docs | Android users & developers |
| [MULTI_PLATFORM_ARCHITECTURE.md](MULTI_PLATFORM_ARCHITECTURE.md) | System design | Developers & architects |
| [MESHTASTIC_GUIDE.md](MESHTASTIC_GUIDE.md) | Meshtastic features | All users |
| [RESTART_GUIDE.md](RESTART_GUIDE.md) | Restart procedures | Administrators |

## License

[Add your license here]

## Support

For issues and questions, please open an issue on GitHub.

---

**Note**: This system is designed for tactical operations and mesh network communication. Ensure proper security measures when deploying in production environments.

## Platform Selection Guide

### Choose iOS PWA if:
- ✅ You're at HQ with reliable internet
- ✅ You need remote access to the system
- ✅ You don't need direct mesh device connection
- ✅ You want automatic updates
- ✅ Quick deployment is priority

### Choose Android Native if:
- ✅ You're in the field without internet
- ✅ You need direct Meshtastic BLE connection
- ✅ You require offline mesh networking
- ✅ GPS tracking is critical
- ✅ Background operation is needed

### Deploy Both for:
- ✅ Complete tactical solution
- ✅ HQ coordination + field operations
- ✅ Redundant communication paths
- ✅ Maximum flexibility

---

## 🔒 Server Federation System

LPU5 implements an ATAK-inspired server federation framework that allows multiple LPU5 nodes (e.g., Raspberry Pi field servers + a central Web server) to securely exchange tactical data after establishing mutual trust.

### Architecture Overview

```
[Field Server A]                  [Web Server (HQ)]
  ┌───────────────┐                 ┌───────────────┐
  │  RSA key pair │  ←── QR scan ─→ │  RSA key pair │
  │  server_id    │                 │  server_id    │
  │  public key   │  challenge  →   │               │
  │               │  ← signature    │               │
  │  TRUSTED ✓    │ ←────────────── │  TRUSTED ✓    │
  └───────────────┘                 └───────────────┘
         ↕  signed CoT / marker sync
```

### How It Works

1. **Key Generation** – Each LPU5 server automatically generates an RSA-2048 key pair (`server_private.pem` / `server_public.pem`) on first boot.
2. **QR Onboarding** – Each server exposes its public key + metadata as a QR code (`GET /api/federation/qr`). The peer server scans this QR to register the server.
3. **Reciprocal Registration** – Both servers register each other's public keys via `POST /api/federation/servers`.
4. **Mutual Handshake** – Server A issues a challenge (`POST /api/federation/servers/{id}/challenge`). Server B signs it with its private key (`POST /api/federation/handshake/respond`) and returns the signature. Server A verifies it (`POST /api/federation/servers/{id}/verify`) and marks B as **trusted**. Both sides repeat this process.
5. **Trusted Data Exchange** – Only trusted peers receive data via `POST /api/federation/sync` (and `POST /api/federation/ingest`). Each payload is RSA-signed to prevent spoofing.

### Federation API Endpoints

All endpoints require `Authorization: Bearer <token>` unless noted.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/federation/info` | Local server public key & metadata |
| `GET` | `/api/federation/qr` | QR code PNG encoding local server info |
| `POST` | `/api/federation/servers` | Register a remote server |
| `GET` | `/api/federation/servers` | List all registered servers |
| `GET` | `/api/federation/servers/{server_id}` | Get specific server details |
| `DELETE` | `/api/federation/servers/{server_id}` | Remove a server |
| `POST` | `/api/federation/servers/{server_id}/challenge` | Issue a challenge to a peer |
| `POST` | `/api/federation/servers/{server_id}/verify` | Verify peer's challenge response → trust |
| `POST` | `/api/federation/handshake/respond` | Sign an incoming challenge with this server's key |
| `POST` | `/api/federation/sync` | Push local markers to all trusted peers (signed) |
| `POST` | `/api/federation/ingest` | Receive & verify signed data from a trusted peer (no auth, key-verified) |

### CLI / API Onboarding Example

```bash
# ── Server A (Field Pi) ──────────────────────────────────────────────────
TOKEN_A="<jwt_from_server_a>"
BASE_A="https://192.168.1.10:8101"

# 1. Get Server A's federation info
curl -sk -H "Authorization: Bearer $TOKEN_A" $BASE_A/api/federation/info

# 2. Display QR code (save PNG and scan with Server B's admin UI)
curl -sk -H "Authorization: Bearer $TOKEN_A" $BASE_A/api/federation/qr -o serverA.png


# ── Server B (Web HQ) ────────────────────────────────────────────────────
TOKEN_B="<jwt_from_server_b>"
BASE_B="https://192.168.1.20:8101"

# 3. Register Server A on Server B (paste info JSON from step 1)
curl -sk -X POST -H "Authorization: Bearer $TOKEN_B" \
  -H "Content-Type: application/json" \
  -d '{"server_id":"<A_server_id>","name":"FieldPi","public_key":"<A_pub_pem>"}' \
  $BASE_B/api/federation/servers

# 4. Server B issues a challenge to Server A's entry
CHALLENGE=$(curl -sk -X POST -H "Authorization: Bearer $TOKEN_B" \
  $BASE_B/api/federation/servers/<A_server_id>/challenge)
CHALLENGE_ID=$(echo $CHALLENGE | python3 -c "import sys,json; print(json.load(sys.stdin)['challenge_id'])")
CHALLENGE_B64=$(echo $CHALLENGE | python3 -c "import sys,json; print(json.load(sys.stdin)['challenge'])")


# ── Back on Server A ─────────────────────────────────────────────────────

# 5. Server A signs the challenge with its private key
SIG=$(curl -sk -X POST -H "Authorization: Bearer $TOKEN_A" \
  -H "Content-Type: application/json" \
  -d "{\"challenge\":\"$CHALLENGE_B64\"}" \
  $BASE_A/api/federation/handshake/respond | python3 -c "import sys,json; print(json.load(sys.stdin)['signature'])")


# ── Back on Server B ─────────────────────────────────────────────────────

# 6. Server B verifies the signature → Server A is now TRUSTED on B
curl -sk -X POST -H "Authorization: Bearer $TOKEN_B" \
  -H "Content-Type: application/json" \
  -d "{\"challenge_id\":\"$CHALLENGE_ID\",\"signature\":\"$SIG\"}" \
  $BASE_B/api/federation/servers/<A_server_id>/verify

# Repeat steps 3-6 with roles reversed so A also trusts B.

# 7. Trigger data sync from Server B to all trusted peers
curl -sk -X POST -H "Authorization: Bearer $TOKEN_B" $BASE_B/api/federation/sync
```

### Key Files

| File | Description |
|------|-------------|
| `server_private.pem` | Local RSA private key (never share!) |
| `server_public.pem` | Local RSA public key (shared via QR / REST) |
| `server_id.txt` | Stable server UUID |
| `federation.py` | Core federation module (key gen, sign, verify, QR) |

### Security Notes

- Private key (`server_private.pem`) is stored **unencrypted on disk** – protect file system access.
- Challenges expire after **120 seconds** and are single-use to prevent replay attacks.
- Only servers with `trusted=true` receive data via `/api/federation/sync`.
- Ingest endpoint (`/api/federation/ingest`) does **not** require JWT; trust is enforced via RSA signature verification against the stored public key.
- All federation events are recorded in the audit log.
