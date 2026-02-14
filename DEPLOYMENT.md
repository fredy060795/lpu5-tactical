# LPU5 Tactical - Multi-Platform Deployment Guide

## Overview

The LPU5 Tactical system is now split into two platform-specific implementations:

1. **iOS PWA (Progressive Web App)** - For iOS devices accessing HQ via REST API
2. **Android Native App** - For Android devices with native Meshtastic mesh integration

## Platform Architecture

### iOS Devices (Web Client Role)
- **Technology**: Progressive Web App (PWA)
- **Access Method**: HQ's public IP address via HTTPS
- **Communication**: REST API + WebSocket
- **Limitations**: No direct Meshtastic BLE (Safari limitation)
- **Use Case**: Remote field devices, HQ coordination
- **Offline**: Service Worker caching for offline operation

### Android Devices (Mesh Client Role)
- **Technology**: Native Android APK with WebView
- **Access Method**: Embedded WebView + Native Meshtastic SDK
- **Communication**: Native BLE/Serial to Meshtastic devices
- **Features**: Full mesh networking, GPS, COT exchange
- **Use Case**: Field operators with direct mesh communication
- **Offline**: Full offline operation with mesh network

---

## iOS PWA Deployment

### Prerequisites
- **Server Requirements**:
  - Python 3.8+ installed
  - SSL certificate (required for HTTPS/PWA)
  - Public IP or domain name accessible from internet
  - Port 8101 (or configured port) open in firewall

- **Client Requirements**:
  - iOS 11.3+ with Safari browser
  - Internet connectivity to reach HQ server

### Server Setup (HQ)

#### 1. Install Dependencies
```bash
# Clone repository
git clone https://github.com/fredy060795/lpu5-tactical.git
cd lpu5-tactical

# Install Python dependencies
pip install -r requirements.txt
```

#### 2. Configure SSL Certificates
```bash
# Option A: Use existing certificates (recommended for production)
# Place your cert.pem and key.pem in the project root

# Option B: Generate self-signed certificate (development/testing)
openssl req -x509 -newkey rsa:4096 -nodes -keyout key.pem -out cert.pem -days 365 \
  -subj "/C=US/ST=State/L=City/O=Organization/CN=your-domain.com"
```

#### 3. Configure API Server
Edit `api.py` if needed to configure:
- Port number (default: 8101)
- Database location
- CORS settings for remote access

#### 4. Start the Server
```bash
# Option A: Using uvicorn directly
uvicorn api:app --host 0.0.0.0 --port 8101 --ssl-keyfile key.pem --ssl-certfile cert.pem

# Option B: Using start script (Windows)
start_lpu5.bat

# Option C: As a service (Linux)
# Create systemd service file at /etc/systemd/system/lpu5-tactical.service
```

Example systemd service:
```ini
[Unit]
Description=LPU5 Tactical API Server
After=network.target

[Service]
Type=simple
User=tactical
WorkingDirectory=/opt/lpu5-tactical
ExecStart=/usr/bin/python3 -m uvicorn api:app --host 0.0.0.0 --port 8101 --ssl-keyfile key.pem --ssl-certfile cert.pem
Restart=always

[Install]
WantedBy=multi-user.target
```

#### 5. Configure Firewall
```bash
# Allow port 8101 through firewall
# For UFW (Ubuntu):
sudo ufw allow 8101/tcp

# For firewalld (CentOS/RHEL):
sudo firewall-cmd --permanent --add-port=8101/tcp
sudo firewall-cmd --reload
```

### iOS Client Installation

#### 1. Access PWA URL
On iOS device, open Safari and navigate to:
```
https://your-server-ip:8101/pwa/overview.html
```

**Important**: You must use HTTPS. HTTP will not work for PWA features.

#### 2. Install PWA to Home Screen
1. Tap the **Share** button (square with arrow)
2. Scroll down and tap **Add to Home Screen**
3. Edit the name if desired (default: "LPU5 Tactical")
4. Tap **Add**

#### 3. Launch the App
- Tap the icon on your home screen
- App will run in standalone mode (no Safari UI)
- Works offline after first load (cached by Service Worker)

### iOS PWA Features
✅ **Available**:
- Map viewing and marker management
- User authentication
- Mission planning
- Real-time updates via WebSocket
- COT message display
- Offline map caching
- Remote HQ communication

❌ **Not Available** (iOS Safari limitations):
- Direct Bluetooth to Meshtastic devices
- Web Bluetooth API
- Background sync

### iOS Limitations & Workarounds

**No Direct Meshtastic Connection**:
- iOS devices communicate with HQ server via REST API
- HQ server can run Meshtastic Gateway Service
- iOS devices receive mesh updates through WebSocket
- Messages sent via API are forwarded to mesh by HQ

**Workaround Setup**:
```bash
# On HQ server, start Meshtastic Gateway
curl -X POST https://localhost:8101/api/gateway/start \
  -H "Content-Type: application/json" \
  -d '{"port": "COM7", "auto_sync": true, "sync_interval": 300}'

# iOS devices will now receive mesh updates through WebSocket
```

---

## Android Native App Deployment

### Prerequisites
- **Development**:
  - Android Studio Arctic Fox or newer
  - JDK 11 or newer
  - Android SDK 24+ (Android 7.0+)
  - Gradle 8.0+

- **Target Devices**:
  - Android 7.0+ (API 24+)
  - Bluetooth Low Energy (BLE) support
  - GPS/Location services

### Building the APK

#### 1. Open Project in Android Studio
```bash
cd lpu5-tactical/android
# Open this directory in Android Studio
```

#### 2. Sync Gradle Dependencies
- Android Studio will prompt to sync Gradle
- Wait for dependencies to download
- Resolve any SDK/NDK version issues if prompted

#### 3. Build APK

**Option A: Debug APK (for testing)**
```bash
# Command line
cd android
./gradlew assembleDebug

# Output: android/app/build/outputs/apk/debug/app-debug.apk
```

**Option B: Release APK (for distribution)**
```bash
# First, create a keystore for signing
keytool -genkey -v -keystore lpu5-tactical.keystore \
  -alias lpu5-key -keyalg RSA -keysize 2048 -validity 10000

# Build release APK
./gradlew assembleRelease

# Output: android/app/build/outputs/apk/release/app-release.apk
```

For release builds, you need to configure signing in `android/app/build.gradle`:
```gradle
android {
    signingConfigs {
        release {
            storeFile file("path/to/lpu5-tactical.keystore")
            storePassword "your-password"
            keyAlias "lpu5-key"
            keyPassword "your-password"
        }
    }
    buildTypes {
        release {
            signingConfig signingConfigs.release
            minifyEnabled false
            proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro'
        }
    }
}
```

### Installing on Android Devices

#### Method 1: USB Installation (ADB)
```bash
# Enable USB debugging on Android device
# Connect device via USB

adb install android/app/build/outputs/apk/debug/app-debug.apk

# Or for release:
adb install android/app/build/outputs/apk/release/app-release.apk
```

#### Method 2: Direct Download
1. Host the APK on a web server
2. Navigate to the URL on Android device
3. Download and tap to install
4. Allow "Install from unknown sources" if prompted

#### Method 3: Google Play Store (Production)
1. Create a Google Play Developer account
2. Prepare store listing (screenshots, description, etc.)
3. Upload signed release APK
4. Complete content rating questionnaire
5. Publish app (review takes 1-3 days)

### Android App First Run

#### 1. Grant Permissions
On first launch, the app will request:
- **Location** - For GPS position tracking
- **Bluetooth** - For Meshtastic BLE connection
- **Nearby Devices** (Android 12+) - For BLE scanning

Grant all permissions for full functionality.

#### 2. Connect to Meshtastic Device
1. Power on your Meshtastic device
2. Ensure Bluetooth is enabled
3. In the app, tap the Meshtastic icon
4. Tap "Connect Device"
5. Select your Meshtastic device from the list
6. Wait for connection (green indicator)

#### 3. Start Using
- Send text messages to mesh network
- Send COT position updates
- View mesh nodes on map
- Track GPS position in real-time

### Android App Features
✅ **Available**:
- Full Web Bluetooth API via native bridge
- Native Meshtastic BLE/Serial communication
- GPS position tracking
- COT message generation and exchange
- Offline mesh operation (no internet required)
- Background location updates
- Native device integration
- Push notifications (future)

### Android Native Bridge API

The Android app provides a JavaScript bridge for WebView communication:

```javascript
// Check if running in native Android app
if (window.isAndroidNative) {
    
    // Connect to Meshtastic device
    window.nativeConnectMeshtastic();
    
    // Send message (text or COT)
    window.nativeSendMessage("Hello mesh!", false);
    window.nativeSendMessage("<event>...</event>", true);
    
    // Get current GPS position
    const position = window.nativeGetPosition();
    // Returns: { latitude: 47.123, longitude: 8.456, altitude: 500, accuracy: 10 }
    
    // Get mesh nodes
    const nodes = window.nativeGetMeshtasticNodes();
    
    // Show native toast
    Android.showToast("Message sent!");
    
    // Listen for native events
    window.onAndroidEvent = function(event, data) {
        if (event === 'locationUpdate') {
            console.log('New position:', data);
        }
        if (event === 'messageSent') {
            console.log('Message sent:', data);
        }
    };
}
```

---

## Security Considerations

### iOS PWA Security
1. **HTTPS Required**: Always use SSL/TLS for PWA features
2. **Authentication**: Implement JWT-based authentication
3. **CORS**: Configure CORS to allow only trusted origins
4. **Rate Limiting**: Implement API rate limiting
5. **Firewall**: Restrict access to known IP ranges if possible

### Android App Security
1. **APK Signing**: Always sign release APKs
2. **Code Obfuscation**: Enable ProGuard for release builds
3. **Permissions**: Request only necessary permissions
4. **Local Storage**: Encrypt sensitive data
5. **Network Security**: Use HTTPS for any external communication

### Meshtastic Security
1. **Encryption**: Enable encryption on Meshtastic devices
2. **Channel Keys**: Use strong channel keys
3. **Admin Channel**: Secure the admin channel separately
4. **Physical Security**: Protect Meshtastic devices from tampering

---

## Troubleshooting

### iOS PWA Issues

**"Cannot connect to server"**
- Check server is running: `curl -k https://server-ip:8101/api/status`
- Verify firewall allows port 8101
- Ensure SSL certificates are valid
- Check device has internet connectivity

**"Add to Home Screen not available"**
- Must use HTTPS (not HTTP)
- Check manifest.json is accessible
- Try hard refresh (hold reload button)
- Update iOS to latest version

**"App doesn't update"**
- Clear Safari cache
- Delete and reinstall PWA
- Check service worker is updated: Developer tools → Service Workers

### Android App Issues

**"Meshtastic service not connected"**
- Ensure Meshtastic SDK dependency is in build.gradle
- Check Bluetooth permissions granted
- Try unbinding and rebinding service
- Restart app

**"Cannot send messages"**
- Verify Meshtastic device is connected and in range
- Check Bluetooth is enabled
- Ensure device is on same channel
- Check LoRa coverage

**"Location not updating"**
- Grant Location permissions
- Enable high-accuracy GPS
- Check device has GPS signal
- Disable battery optimization for app

**"Build errors"**
- Clean project: `./gradlew clean`
- Invalidate caches in Android Studio
- Update Gradle dependencies
- Check internet connection for dependency download

---

## Maintenance & Updates

### Updating iOS PWA
1. Update files on server
2. Increment service worker version in `sw.js`
3. Restart server
4. iOS devices will auto-update on next connection

### Updating Android App
1. Update code in Android Studio
2. Increment `versionCode` and `versionName` in `app/build.gradle`
3. Build new APK
4. Distribute via previous methods
5. Users install update manually (or via Play Store auto-update)

---

## Support & Resources

### Official Documentation
- [LPU5 Tactical README](../README.md)
- [Meshtastic Documentation](https://meshtastic.org/docs/)
- [Android Developer Guide](https://developer.android.com/)
- [iOS PWA Guide](https://developer.apple.com/library/archive/documentation/AppleApplications/Reference/SafariWebContent/ConfiguringWebApplications/ConfiguringWebApplications.html)

### Common Commands
```bash
# Check API server status
curl -k https://localhost:8101/api/status

# View server logs
tail -f /var/log/lpu5-tactical.log

# Check Android device connection
adb devices

# View Android app logs
adb logcat | grep LPU5
```

### Getting Help
- GitHub Issues: [Repository Issues](https://github.com/fredy060795/lpu5-tactical/issues)
- Meshtastic Discord: https://discord.gg/meshtastic
- ATAK Forum: https://tak.gov/

---

## Appendix: Platform Comparison

| Feature | iOS PWA | Android Native |
|---------|---------|----------------|
| **Deployment** | Web server | APK installation |
| **Updates** | Automatic | Manual/Play Store |
| **Meshtastic** | Via HQ Gateway | Direct BLE/Serial |
| **Offline** | Limited (cached) | Full mesh offline |
| **GPS** | Browser API | Native API |
| **COT** | Display only | Send & Receive |
| **Background** | Limited | Full support |
| **Installation** | Add to Home | APK install |
| **Size** | ~500KB cached | ~15MB APK |
| **Updates** | Instant | Version-based |

**Recommendation**: 
- Use **iOS PWA** for HQ personnel and remote coordination
- Use **Android Native** for field operators requiring mesh communication
