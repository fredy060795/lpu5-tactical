# LPU-5-system

## LPU5 Tactical Tracker

A comprehensive tactical tracking system with integrated Meshtastic support for real-time mesh network communication, COT (Cursor on Target) protocol, and position tracking. Now includes a fully off-grid Progressive Web App (PWA) integration for direct device connectivity.

## Features

### Core Features
- **Real-time Map Display**: Live tactical map with markers, drawings, and overlays
- **User Management**: Role-based access control with authentication
- **Mission Planning**: Create and manage tactical missions
- **WebSocket Support**: Real-time updates across all connected clients
- **QR Code System**: Secure access via QR codes with expiration and usage limits
- **PWA Support**: Installable as offline-first Progressive Web App

### Meshtastic PWA Integration (New) 🆕
The integrated Meshtastic PWA enables **fully off-grid** operation with direct Bluetooth connectivity to Meshtastic devices. Works completely without internet or backend server.

#### PWA Features:
- **Web Bluetooth**: Direct BLE connection to Meshtastic devices (no backend required)
- **Off-Grid Operation**: 100% offline capable - no internet connection needed
- **COT Protocol**: Full ATAK/WinTAK compatibility for tactical coordination
- **Offline Queue**: IndexedDB-based message queue with automatic retry
- **Real-time Map**: Mesh nodes and COT messages displayed on Leaflet map
- **Cross-Platform**: Works on Android, iOS, Windows, ChromeOS
- **PWA Installable**: Add to home screen for native-like experience
- **Easy Distribution**: Static HTML files - no server installation needed

#### Quick Start (PWA):
1. Open `overview.html` in Chrome/Edge/Opera
2. Click "Install" or "Add to Home Screen"
3. Launch the app (works offline!)
4. Click the Meshtastic icon (green mesh circle)
5. Click "Connect Device" and select your Meshtastic device
6. Send text or COT messages via LoRa mesh network

See [MESHTASTIC_GUIDE.md](MESHTASTIC_GUIDE.md) for detailed instructions.

### Meshtastic Gateway Service (Backend)
The integrated Meshtastic Gateway Service enables real-time hardware connection to Meshtastic devices for automatic data import and live tracking.

#### Gateway Features:
- **Hardware Connection**: Direct serial port connection to Meshtastic devices
- **Auto-Sync**: Automatic synchronization of nodes and messages
- **Real-time Updates**: Live WebSocket broadcasts for position updates
- **Message Handling**: Send and receive messages through the gateway
- **Node Tracking**: Automatic import of discovered Meshtastic nodes with GPS data

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
curl -X POST http://localhost:8001/api/gateway/start \
  -H "Content-Type: application/json" \
  -d '{"port": "COM7", "auto_sync": true, "sync_interval": 300}'

# Check status
curl http://localhost:8001/api/gateway/status

# Stop gateway
curl -X POST http://localhost:8001/api/gateway/stop
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
uvicorn api:app --host 0.0.0.0 --port 8001 --ssl-keyfile key.pem --ssl-certfile cert.pem
```

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
const ws = new WebSocket('wss://localhost:8001/ws');
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

## PWA Browser Compatibility

### Fully Supported ✅
- **Chrome (Android, Windows, ChromeOS)**: Full Web Bluetooth and PWA support
- **Edge (Windows)**: Full Web Bluetooth and PWA support
- **Opera (Android, Windows)**: Full Web Bluetooth and PWA support

### Partial Support ⚠️
- **Chrome (macOS, Linux)**: Web Bluetooth available with limitations
- **iOS Safari**: Limited - no Web Bluetooth support (use backend gateway instead)

### Not Supported ❌
- **Firefox**: Web Bluetooth not implemented
- **Older Browsers**: Requires Chrome 56+ or equivalent

For detailed compatibility and troubleshooting, see [MESHTASTIC_GUIDE.md](MESHTASTIC_GUIDE.md).

## Development

### Project Structure
```
lpu5-tactical/
├── api.py                          # Main API server
├── meshtastic_gateway_service.py   # Backend gateway service
├── websocket_manager.py            # WebSocket handler
├── database.py                     # Database connection
├── models.py                       # SQLAlchemy models
├── meshtastic.html                 # Backend gateway UI
├── overview.html                   # PWA with Meshtastic integration
├── meshtastic-web-client.js        # Web Bluetooth client
├── cot-client.js                   # COT protocol implementation
├── message-queue-manager.js        # Offline message queue
├── MESHTASTIC_GUIDE.md             # User guide for PWA integration
├── MESHTASTIC_TECHNICAL.md         # Technical documentation
├── index.html                      # Main map UI
├── requirements.txt                # Python dependencies
└── README.md                       # This file
```

### Adding Features
1. API endpoints: Add to `api.py`
2. Database models: Add to `models.py`
3. Gateway features: Modify `meshtastic_gateway_service.py`
4. UI: Update relevant HTML files

## License

[Add your license here]

## Support

For issues and questions, please open an issue on GitHub.

---

**Note**: This system is designed for tactical operations and mesh network communication. Ensure proper security measures when deploying in production environments.