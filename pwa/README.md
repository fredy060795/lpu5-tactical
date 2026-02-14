# LPU5 Tactical - iOS Progressive Web App

## Overview

Progressive Web App (PWA) version of LPU5 Tactical optimized for iOS devices. This implementation provides access to the tactical network system via HQ's REST API, designed to work around iOS Safari's lack of Web Bluetooth API support.

## Architecture

Unlike the Android native app, the iOS PWA operates as a **web client** that communicates with the HQ server:

```
iOS Device (Safari PWA) 
    ↕ HTTPS/WebSocket
HQ Server (REST API + Gateway)
    ↕ Serial/BLE
Meshtastic Device
    ↕ LoRa Mesh
Field Devices
```

## Features

### Available Features ✅
- **Progressive Web App** - Install to home screen, works offline
- **Map Display** - Interactive Leaflet.js map with markers
- **User Authentication** - Secure JWT-based login
- **Real-time Updates** - WebSocket connection to HQ
- **COT Message Display** - View ATAK-compatible messages
- **Mission Planning** - Create and view missions
- **Offline Caching** - Service Worker caches assets
- **Marker Management** - Create, edit, delete markers
- **Mesh Node Display** - View nodes via HQ gateway

### Limitations ❌
- **No Direct Bluetooth** - iOS Safari doesn't support Web Bluetooth API
- **Requires Internet** - Must connect to HQ server
- **Gateway Dependent** - Mesh access via HQ's Meshtastic gateway
- **Limited Background** - iOS restricts background web apps
- **No Native Integration** - Cannot access native BLE or serial

## Requirements

### Server Requirements (HQ)
- **Python 3.8+** installed
- **SSL Certificate** (HTTPS required for PWA)
- **Public IP** or domain name
- **Port 8101** accessible from internet
- **Meshtastic Gateway** (optional, for mesh integration)

### Client Requirements (iOS Device)
- **iOS 11.3+** (Safari)
- **Internet Connection** (WiFi or cellular)
- **Safari Browser** (default iOS browser)

## Installation

### Server Setup (One-time HQ configuration)

See [DEPLOYMENT.md](../DEPLOYMENT.md#ios-pwa-deployment) for detailed server setup instructions.

Quick start:
```bash
# On HQ server
cd lpu5-tactical
pip install -r requirements.txt

# Start API server
uvicorn api:app --host 0.0.0.0 --port 8101 \
  --ssl-keyfile key.pem --ssl-certfile cert.pem
```

### iOS Device Installation

#### 1. Navigate to PWA URL
Open Safari and go to:
```
https://your-hq-server-ip:8101/pwa/overview.html
```

**Important**: 
- Must use **HTTPS** (not HTTP)
- Accept SSL certificate if self-signed
- Bookmark URL for easy access

#### 2. Add to Home Screen
1. Tap the **Share** button (square with arrow pointing up)
2. Scroll down in the share sheet
3. Tap **"Add to Home Screen"**
4. Edit name if desired (default: "LPU5 Tactical")
5. Tap **"Add"** in top right

#### 3. Launch App
- Tap the icon on your home screen
- App opens in standalone mode (no Safari UI)
- Login with your credentials
- Start using the tactical map

## Configuration

### Manifest Configuration
The PWA manifest is located at `pwa/manifest.json`:

```json
{
  "name": "LPU5 Tactical Network - iOS PWA",
  "short_name": "LPU5-iOS",
  "start_url": "/pwa/overview.html",
  "display": "standalone",
  "theme_color": "#28a745",
  "background_color": "#0a0a0a"
}
```

**iOS-Specific Settings:**
- `apple-mobile-web-app-capable`: Enables standalone mode
- `apple-mobile-web-app-status-bar-style`: Status bar appearance
- `apple-mobile-web-app-title`: Home screen icon name

### Service Worker
Location: `pwa/sw.js`

**Caching Strategy:**
- **Static assets**: Cache-first (HTML, CSS, JS)
- **API calls**: Network-first (always fresh data)
- **Map tiles**: Cache with fallback

**Cache Version:**
Update version when deploying changes:
```javascript
const CACHE_VERSION = 'v2';
```

## Usage

### Logging In
1. Launch app from home screen
2. Enter username and password
3. Tap "Login"
4. JWT token stored for session

### Viewing Map
- Pan and zoom map with gestures
- Tap markers for information
- Your position shown (if GPS enabled in Safari)
- Mesh nodes displayed (if HQ gateway active)

### Sending Messages (via HQ)
Since iOS cannot directly connect to Meshtastic:

**Option 1: Via Gateway API**
```javascript
// If HQ has gateway running
fetch('/api/gateway/send-message', {
    method: 'POST',
    headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
    },
    body: JSON.stringify({
        message: "Hello from iOS",
        type: "text"
    })
});
```

**Option 2: Via HQ User**
- HQ personnel relay messages
- WebSocket broadcasts to all clients
- iOS devices receive updates in real-time

### Receiving Updates
Updates pushed via WebSocket:
```javascript
const ws = new WebSocket('wss://hq-server:8101/ws');
ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    
    switch(data.type) {
        case 'marker_update':
            // Update map marker
            break;
        case 'gateway_message':
            // Display mesh message
            break;
        case 'gateway_node_update':
            // Update node position
            break;
    }
};
```

### Offline Mode
**What Works Offline:**
- View cached map at last position
- Browse previously loaded markers
- Read message history
- UI remains functional

**What Doesn't Work Offline:**
- New marker creation (queued)
- Real-time updates
- Authentication
- API calls
- WebSocket connection

**Automatic Sync:**
When connection restored:
- Queued actions are sent
- Latest data fetched
- Map refreshed

## File Structure

```
pwa/
├── overview.html              # Main PWA HTML
├── manifest.json              # PWA manifest (iOS optimized)
├── sw.js                      # Service Worker
├── logo.png                   # App icon
├── meshtastic-web-client.js   # Meshtastic client library
├── cot-client.js              # COT protocol handler
└── message-queue-manager.js   # Offline queue
```

## API Integration

### REST Endpoints Used

**Authentication:**
```
POST /api/login_user
POST /api/register_user
GET /api/me
```

**Map & Markers:**
```
GET /api/map_markers
POST /api/map_markers
PUT /api/map_markers/{id}
DELETE /api/map_markers/{id}
```

**Meshtastic Gateway (if available):**
```
GET /api/gateway/status
GET /api/gateway/nodes
GET /api/gateway/messages
POST /api/gateway/send-message
```

### WebSocket Events

Connect to WebSocket for real-time updates:
```javascript
const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

ws.onopen = () => console.log('Connected to HQ');
ws.onmessage = (event) => handleUpdate(JSON.parse(event.data));
ws.onerror = (error) => console.error('WebSocket error:', error);
ws.onclose = () => console.log('Disconnected from HQ');
```

## Troubleshooting

### Cannot Add to Home Screen
**Causes:**
- Not using HTTPS
- Manifest.json not accessible
- Safari cache issues

**Solutions:**
- Verify HTTPS URL (check padlock icon)
- Test manifest: `/pwa/manifest.json`
- Clear Safari cache: Settings → Safari → Clear History and Website Data
- Try private browsing first

### App Won't Load Offline
**Causes:**
- Service Worker not registered
- Cache incomplete
- iOS cleared cache

**Solutions:**
- Ensure you loaded app while online first
- Check service worker: Safari → Develop → Service Workers
- Reinstall PWA: Delete from home screen and re-add
- Browse all sections while online to cache them

### Connection to HQ Failed
**Causes:**
- Server offline
- Firewall blocking port
- SSL certificate invalid
- Internet connection issues

**Solutions:**
- Ping HQ server
- Check server logs
- Verify port 8101 is open
- Test from another device
- Check cellular/WiFi connection

### Real-time Updates Not Working
**Causes:**
- WebSocket connection failed
- Server not broadcasting
- iOS background restrictions

**Solutions:**
- Check WebSocket status in console
- Keep app in foreground
- Verify server WebSocket endpoint
- Restart app

### Features Missing vs Android
**This is expected!**

iOS PWA is intentionally limited due to Safari constraints:
- No Web Bluetooth (Apple policy)
- Limited background processing
- No native hardware access
- Internet connection required

**Recommendation:**
For field operators needing direct mesh communication, use Android native app instead.

## Development

### Local Testing
```bash
# Start development server
cd lpu5-tactical
python -m http.server 8000 --bind 0.0.0.0

# Access from iOS device on same network
# https://your-computer-ip:8000/pwa/overview.html
```

### Debugging
Enable Safari Developer Mode:
1. iOS: Settings → Safari → Advanced → Web Inspector (ON)
2. Mac: Safari → Preferences → Advanced → Show Develop menu (check)
3. Connect iOS device via USB
4. Mac Safari → Develop → [Your Device] → [Page]
5. Use Web Inspector to debug

### Console Logging
```javascript
// View logs in Safari inspector
console.log('iOS PWA running');
console.error('Error details');

// Remote debugging
if (window.isStandalone) {
    console.log('Running as PWA');
} else {
    console.log('Running in Safari');
}
```

## Performance Optimization

### Reduce Initial Load
- Minify HTML/CSS/JS
- Compress images
- Use CDN for libraries
- Enable gzip compression

### Improve Offline Experience
- Cache map tiles proactively
- Store last known position
- Queue user actions
- Show offline indicator

### Battery Optimization
- Reduce WebSocket ping interval
- Pause updates when inactive
- Use passive event listeners
- Minimize GPS usage

## Security

### HTTPS Enforcement
PWA features require HTTPS:
- Service Workers
- Geolocation API
- Add to Home Screen
- Secure cookies

### Authentication
JWT tokens with expiration:
```javascript
// Store token
localStorage.setItem('auth_token', token);

// Include in requests
headers: {
    'Authorization': `Bearer ${token}`
}

// Handle expiration
if (response.status === 401) {
    // Redirect to login
}
```

### Data Protection
- Sensitive data in HTTPS only
- No local password storage
- Tokens expire after inactivity
- Clear cache on logout

## Comparison: iOS PWA vs Android Native

| Feature | iOS PWA | Android Native |
|---------|---------|----------------|
| Installation | Add to Home | APK Install |
| Bluetooth | ❌ No | ✅ Yes |
| GPS | Browser API | Native API |
| Offline Mesh | ❌ No | ✅ Yes |
| Internet Required | ✅ Yes | ❌ No |
| Update Method | Automatic | Manual/Store |
| Background | Limited | Full |
| Hardware Access | None | Full |
| Distribution | Web URL | APK/Play Store |
| Size | ~500KB | ~15MB |

**Use iOS PWA when:**
- At HQ with reliable internet
- Remote coordination needed
- Quick deployment required
- No direct mesh access needed

**Use Android Native when:**
- In field without internet
- Direct mesh communication required
- GPS tracking critical
- Background operation needed

## Support

### Documentation
- [Main README](../README.md) - Project overview
- [DEPLOYMENT.md](../DEPLOYMENT.md) - Complete deployment guide
- [ARCHITECTURE.md](../MULTI_PLATFORM_ARCHITECTURE.md) - System architecture

### Getting Help
- **Issues**: [GitHub Issues](https://github.com/fredy060795/lpu5-tactical/issues)
- **Email**: [Your support email]
- **Forum**: [Your support forum]

### Known Issues
1. **iOS 14.x**: Service Worker may not persist
   - Workaround: Reinstall PWA after iOS updates
   
2. **iPad Split View**: Map gestures may conflict
   - Workaround: Use full-screen mode

3. **Low Power Mode**: WebSocket disconnects
   - Workaround: Disable Low Power Mode or use manual refresh

## Future Enhancements

### Planned Features
- [ ] Push notifications (via server)
- [ ] Improved offline map caching
- [ ] Voice memo support
- [ ] Enhanced COT visualization
- [ ] Multi-language support
- [ ] Dark mode toggle
- [ ] Haptic feedback

### Requested Features
- [ ] Apple Watch companion
- [ ] Siri shortcuts
- [ ] iCloud sync
- [ ] CarPlay integration

## License

[Specify license]

---

**For Android native app, see**: [../android/README.md](../android/README.md)
