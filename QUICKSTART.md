# LPU5 Tactical - Quick Start Guide

## Choose Your Platform

### 🍎 iOS Users (Field Command / HQ)
**Best for**: Remote access, HQ coordination, internet-connected environments

**Installation** (5 minutes):
1. Get the HQ server IP/domain from your administrator
2. Open **Safari** on your iOS device
3. Navigate to: `https://[HQ-IP]:8101/pwa/overview.html`
4. Tap the **Share** button (□↑)
5. Scroll and tap **"Add to Home Screen"**
6. Tap **"Add"**
7. Launch from your home screen

**What you can do**:
- ✅ View real-time tactical map
- ✅ Create and manage markers
- ✅ Receive mesh updates (via HQ gateway)
- ✅ Send messages through HQ
- ✅ Mission planning
- ✅ Works offline (cached)

**Limitations**:
- ❌ No direct Bluetooth to Meshtastic
- ❌ Requires internet to HQ server
- ❌ Limited background operation

👉 **Detailed guide**: [pwa/README.md](pwa/README.md)

---

### 🤖 Android Users (Field Operations)
**Best for**: Direct mesh communication, offline operations, field work

**Installation** (10 minutes):
1. Download the APK from your administrator
2. Enable **"Install from unknown sources"** in Settings
3. Install the APK
4. Grant **Location** and **Bluetooth** permissions
5. Open the app
6. Connect to your Meshtastic device

**What you can do**:
- ✅ Direct Bluetooth to Meshtastic devices
- ✅ Full offline mesh networking
- ✅ Send/receive LoRa messages
- ✅ GPS position tracking
- ✅ COT message exchange
- ✅ Background operation
- ✅ Works 100% offline

**Requirements**:
- Android 7.0+ (API 24+)
- Bluetooth Low Energy (BLE)
- GPS (recommended)

👉 **Detailed guide**: [android/README.md](android/README.md)

---

### 🖥️ Administrators (HQ Server Setup)
**For**: Setting up the backend server and gateway

**Quick Setup** (15 minutes):
```bash
# 1. Clone repository
git clone https://github.com/fredy060795/lpu5-tactical.git
cd lpu5-tactical

# 2. Install dependencies
pip install -r requirements.txt

# 3. Generate SSL certificate (or use existing)
openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout key.pem -out cert.pem -days 365

# 4. Start the server
uvicorn api:app --host 0.0.0.0 --port 8101 \
  --ssl-keyfile key.pem --ssl-certfile cert.pem

# 5. Optional: Start Meshtastic Gateway
curl -X POST http://localhost:8101/api/gateway/start \
  -H "Content-Type: application/json" \
  -d '{"port": "COM7", "auto_sync": true}'
```

**Server Requirements**:
- Python 3.8+
- SSL certificate
- Public IP or domain
- Port 8101 accessible

👉 **Detailed guide**: [DEPLOYMENT.md](DEPLOYMENT.md)

---

## First Time Usage

### iOS PWA - First Steps
1. **Launch** the app from home screen
2. **Login** with credentials provided by admin
3. **View map** - your tactical situation
4. **Tap Meshtastic icon** to see mesh status
5. **Create markers** by tapping on map

### Android Native - First Steps
1. **Launch** the app
2. **Grant permissions** when prompted
3. **Tap Meshtastic icon** (bottom toolbar)
4. **Tap "Connect Device"**
5. **Select** your Meshtastic device from list
6. **Wait** for green indicator (connected)
7. **Send test message** via mesh

---

## Common Tasks

### Sending a Message

**iOS PWA** (via HQ):
```
1. Ensure connected to HQ (check WiFi/data)
2. Open message panel
3. Type message
4. Send (forwarded through HQ gateway)
```

**Android Native** (direct mesh):
```
1. Ensure Meshtastic connected (green indicator)
2. Tap Meshtastic panel
3. Type message
4. Uncheck "Send as COT" for text
5. Tap "Send"
```

### Sending Position (COT)

**iOS PWA**:
```
1. Create marker on map
2. Marker synced to HQ
3. HQ can forward to mesh via gateway
```

**Android Native**:
```
1. Tap Meshtastic panel
2. Type description/callsign
3. Check "Send as COT"
4. Tap "Send"
5. Your GPS position included automatically
```

### Viewing Mesh Nodes

**iOS PWA**:
```
- Nodes visible if HQ has gateway running
- Displayed as markers on map
- Updated via WebSocket
```

**Android Native**:
```
- Tap Meshtastic panel
- See list of discovered nodes
- Nodes with GPS shown as blue markers on map
```

---

## Troubleshooting

### iOS PWA

**"Cannot connect to server"**
- Check WiFi/cellular connection
- Verify HQ server is running
- Check URL is correct (https://)
- Accept SSL certificate if prompted

**"App not updating"**
- Pull down to refresh
- Close and reopen app
- Check internet connection
- Delete and reinstall if needed

### Android Native

**"Meshtastic not connecting"**
- Check Bluetooth is enabled
- Ensure device is powered on and in range
- Try restarting Meshtastic device
- Reconnect from app

**"No GPS position"**
- Grant location permissions
- Go outside for clear sky view
- Enable high-accuracy mode
- Wait 30-60 seconds for GPS fix

**"Messages not sending"**
- Verify Meshtastic connected
- Check LoRa coverage
- Ensure same channel as other devices
- Check device battery level

---

## Platform Comparison

| Feature | iOS PWA | Android Native |
|---------|:-------:|:--------------:|
| **Installation** | Easy (web) | Medium (APK) |
| **Direct Bluetooth** | ❌ | ✅ |
| **Offline Mesh** | ❌ | ✅ |
| **Internet Needed** | ✅ | ❌ |
| **Battery Impact** | Low | Medium |
| **Background** | Limited | Full |
| **Updates** | Automatic | Manual |
| **Complexity** | Simple | Moderate |

---

## When to Use Each Platform

### Use iOS PWA when:
- ✅ You're at HQ or have reliable internet
- ✅ You need remote access to the system
- ✅ You don't have a Meshtastic device
- ✅ You want easy installation/updates
- ✅ Battery life is critical

### Use Android Native when:
- ✅ You're in the field without internet
- ✅ You have a Meshtastic device
- ✅ You need direct mesh communication
- ✅ GPS tracking is required
- ✅ Background operation is needed

### Use Both when:
- ✅ You want maximum flexibility
- ✅ HQ + field coordination needed
- ✅ Redundant communication paths
- ✅ Complete tactical solution

---

## System Roles

### HQ Role (iOS PWA)
- **Mission**: Command and control
- **Connectivity**: Internet/WiFi
- **Device**: iOS tablet/phone
- **Access**: Web-based, remote
- **Updates**: Automatic

### Field Role (Android Native)
- **Mission**: Field operations
- **Connectivity**: Mesh only, offline
- **Device**: Android phone with GPS
- **Access**: Native app, local
- **Updates**: Manual APK

### Gateway Role (Server)
- **Mission**: Bridge iOS ↔ Mesh
- **Connectivity**: Internet + Serial/BLE
- **Device**: Server with Meshtastic
- **Access**: API + Hardware
- **Updates**: Manual deployment

---

## Getting Help

### Documentation
- **[README.md](README.md)** - Project overview
- **[DEPLOYMENT.md](DEPLOYMENT.md)** - Complete deployment guide
- **[pwa/README.md](pwa/README.md)** - iOS-specific guide
- **[android/README.md](android/README.md)** - Android-specific guide
- **[MULTI_PLATFORM_ARCHITECTURE.md](MULTI_PLATFORM_ARCHITECTURE.md)** - Technical architecture

### Support Channels
- **GitHub Issues**: Report bugs or request features
- **Email**: [Your support email]
- **Documentation**: Check guides above first

### Common Questions

**Q: Can I use both iOS PWA and Android Native?**
A: Yes! Use iOS PWA when at HQ, Android Native in the field.

**Q: Does iOS PWA work completely offline?**
A: Partially. UI is cached, but you need internet to sync data.

**Q: Can Android Native access HQ server?**
A: Yes, but it's designed primarily for direct mesh use.

**Q: What if I don't have a Meshtastic device?**
A: Use iOS PWA to access the system through HQ server.

**Q: How do I update the apps?**
A: iOS PWA updates automatically. Android requires new APK installation.

**Q: Is this secure for tactical operations?**
A: Yes, with proper setup: HTTPS, JWT auth, Meshtastic encryption.

---

## Next Steps

### For iOS Users
1. ✅ Install PWA following steps above
2. ✅ Login with provided credentials
3. ✅ Read [pwa/README.md](pwa/README.md)
4. ✅ Practice creating markers
5. ✅ Test offline mode

### For Android Users
1. ✅ Install APK from administrator
2. ✅ Connect Meshtastic device
3. ✅ Read [android/README.md](android/README.md)
4. ✅ Test sending messages
5. ✅ Verify GPS tracking

### For Administrators
1. ✅ Set up HQ server
2. ✅ Configure SSL certificates
3. ✅ Read [DEPLOYMENT.md](DEPLOYMENT.md)
4. ✅ Set up Meshtastic gateway (optional)
5. ✅ Create user accounts
6. ✅ Distribute credentials/APK

---

**Ready to get started? Choose your platform above and follow the quick installation steps!**
