# Meshtastic PWA Integration - User Guide

## Overview
The LPU5 Tactical system now includes a fully off-grid Meshtastic integration that works completely offline using Web Bluetooth and LoRa mesh networking. This guide will help you use the Meshtastic features effectively.

## Features

### 🔌 Direct Device Connection
- Connect directly to Meshtastic devices via Bluetooth
- No internet or backend server required
- Works on Android, Windows, ChromeOS (Chrome/Edge/Opera browsers)
- Real-time mesh network communication

### 📡 COT Protocol Support
- Send and receive COT (Cursor on Target) messages
- Compatible with ATAK/WinTAK
- Automatic map visualization
- XML generation and parsing

### 💾 Offline Message Queue
- Messages are queued when device is disconnected
- Automatic retry on failure (3 attempts)
- Persistent storage with IndexedDB
- Export/backup functionality

### 🗺️ Map Integration
- Mesh nodes shown as blue markers
- COT messages displayed as green markers
- Real-time position updates
- Click markers for detailed information

## Getting Started

### Prerequisites
1. **Browser Requirements**:
   - Chrome 56+ (Android, Windows, ChromeOS)
   - Edge 79+ (Windows)
   - Opera 43+ (Android, Windows)
   - **Note**: iOS Safari has limited Bluetooth support

2. **Hardware Requirements**:
   - Meshtastic-compatible device (T-Beam, Heltec, LILYGO, etc.)
   - Device must have Bluetooth enabled
   - Properly configured Meshtastic firmware

### Installation as PWA

1. **Open overview.html** in a supported browser
2. **Install the app**:
   - On Android: Tap the menu → "Add to Home Screen"
   - On Desktop: Click the install icon in the address bar
3. **Launch** from your home screen - works completely offline!

## Using Meshtastic Features

### Connecting to a Device

1. **Open the Meshtastic Panel**:
   - Click the mesh icon (green circle with checkmark) in the bottom toolbar
   - Or use the mesh icon in the top navigation

2. **Pair Your Device**:
   - Click "Connect Device" button
   - A Bluetooth device picker will appear
   - Select your Meshtastic device from the list
   - Wait for the connection to establish

3. **Connection Status**:
   - 🔴 Gray dot: Not connected
   - 🟠 Orange dot (pulsing): Connecting
   - 🟢 Green dot (pulsing): Connected

### Sending Messages

#### Text Messages
1. Open the Meshtastic panel
2. Type your message in the text area
3. Ensure "Send as COT" is **unchecked**
4. Click "Send"
5. Message will be sent via LoRa to all mesh nodes

#### COT Messages
1. Open the Meshtastic panel
2. Type your message or description
3. **Check** "Send as COT"
4. Click "Send"
5. A COT XML message will be generated with your current map position
6. Recipients will see your position as a marker on their map

#### Manual COT XML
1. Create COT XML manually (or copy from ATAK)
2. Paste into the message area
3. Click "Send"
4. XML will be transmitted as-is

### Receiving Messages

#### Automatic Processing
- All incoming messages appear in the chat window
- COT messages are automatically parsed
- Markers are added to the map for COT positions
- Messages are stored in offline database

#### Message Types
- **Text messages**: Gray background, sender name shown
- **COT messages**: Green "📡 COT Message" indicator
- **Your messages**: Blue background on the right

### Viewing Mesh Nodes

The Meshtastic panel shows discovered nodes:
- **Node name**: Green text (e.g., "Alpha-1")
- **Role**: CLIENT, ROUTER, ROUTER_CLIENT, etc.
- **Position indicator**: 📍 if has GPS, ❓ if position unknown
- Nodes with GPS coordinates appear as blue markers on map

### Offline Queue Management

#### Queue Statistics
The panel shows:
- **Pending**: Messages waiting to be sent
- **Sent**: Successfully transmitted messages
- **Received**: Messages from mesh network

#### Automatic Retry
- Failed messages are retried up to 3 times
- 30-second interval between retries
- After 3 failures, message is marked as failed

#### Export Data
1. Click "Export Data" button
2. Downloads JSON file with all:
   - Pending messages
   - Sent messages
   - Received messages
   - Known mesh nodes
3. Use for backup or analysis

## Browser Compatibility

### ✅ Fully Supported
- **Chrome (Android)**: Full Web Bluetooth support
- **Chrome (Windows)**: Full Web Bluetooth support
- **Chrome (ChromeOS)**: Full Web Bluetooth support
- **Edge (Windows)**: Full Web Bluetooth support
- **Opera (Android)**: Full Web Bluetooth support

### ⚠️ Partial Support
- **Chrome (macOS)**: Web Bluetooth available but some limitations
- **Chrome (Linux)**: Requires BlueZ 5.41+ and experimental flag

### ❌ Not Supported
- **iOS Safari**: Web Bluetooth not available
- **Firefox**: Web Bluetooth not implemented
- **Older browsers**: Require Chrome 56+ or equivalent

## Troubleshooting

### "Web Bluetooth not supported"
**Solution**: Use Chrome, Edge, or Opera browser on Android/Windows/ChromeOS

### "Connection failed" or "cancelled by user"
**Causes**:
- Bluetooth is disabled on device
- Meshtastic device is off or out of range
- User cancelled pairing dialog
- Another app is using the device

**Solutions**:
- Enable Bluetooth on your computer/phone
- Power on your Meshtastic device
- Move closer to the device
- Close other apps using Bluetooth
- Restart your Meshtastic device

### Messages not sending
**Possible causes**:
- Device disconnected
- Poor LoRa coverage
- Channel mismatch

**Solutions**:
- Check connection status (green dot)
- Reconnect if needed
- Messages are queued and will send when connected
- Check your Meshtastic device channel settings

### Nodes not appearing
**Causes**:
- Nodes haven't transmitted position yet
- Out of LoRa range
- Node doesn't have GPS

**Solutions**:
- Wait for nodes to broadcast (usually every 15-30 minutes)
- Move to area with better mesh coverage
- Some nodes may not have GPS and won't show on map

### Can't install as PWA
**Solutions**:
- Ensure you're using HTTPS (or localhost)
- Check that manifest.json is accessible
- Try installing from the browser menu
- On Android: Use "Add to Home Screen" from Chrome menu

## Tips and Best Practices

### Battery Conservation
- Disconnect when not actively using mesh network
- Meshtastic devices have excellent battery life but Bluetooth uses phone battery
- Messages are queued offline and sent when you reconnect

### Message Privacy
- All messages are broadcast to the mesh network
- Use appropriate encryption settings on your Meshtastic device
- Don't send sensitive information over mesh

### COT Message Efficiency
- COT messages are larger than text messages
- Use text for quick updates
- Use COT when position sharing is important
- Consider the slower LoRa data rate (300-5500 bps)

### Offline Operation
- Download map tiles while online (zoom/pan to areas you need)
- Export your data regularly for backup
- The app works 100% offline once installed as PWA

### Multi-Device Usage
- You can install on multiple devices
- Each device can connect to the same Meshtastic device
- Only one Bluetooth connection at a time
- Use for backup/redundancy

## Advanced Features

### Creating Custom COT Messages
You can create advanced COT XML manually:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="MESH-USER-001" type="a-f-G-U-C" how="m-g" 
       time="2024-01-01T12:00:00Z" start="2024-01-01T12:00:00Z" 
       stale="2024-01-01T12:05:00Z">
  <point lat="47.1234" lon="8.5678" hae="500" ce="10" le="5"/>
  <detail>
    <contact callsign="Alpha-1"/>
    <__group name="Team Alpha" role="Team Lead"/>
    <remarks>Rally point established</remarks>
    <track speed="0.0" course="0.0"/>
  </detail>
</event>
```

### Importing Backup Data
Currently export-only. Future version may support import.

### API Access
The integration exposes global JavaScript functions:
- `toggleMeshtasticPanel()` - Open/close panel
- `connectMeshtastic()` - Connect to device
- `disconnectMeshtastic()` - Disconnect
- `sendMeshtasticMessage()` - Send message
- `exportMeshData()` - Export data

## Technical Specifications

### Protocols
- **Meshtastic Protocol**: Via Web Bluetooth
- **COT Protocol**: Version 2.0 (ATAK compatible)
- **LoRa Modulation**: Configured on device
- **Bluetooth**: BLE (Bluetooth Low Energy)

### Storage
- **Technology**: IndexedDB
- **Databases**:
  - pendingMessages: Outgoing message queue
  - sentMessages: Transmission history
  - receivedMessages: Incoming messages
  - nodes: Mesh network nodes
- **Retention**: Configurable (default: 7 days auto-cleanup)

### Performance
- **Message latency**: 1-30 seconds (depends on LoRa settings)
- **Range**: 1-10+ km (line of sight with LoRa)
- **Battery**: Device-dependent, typically days on Meshtastic hardware
- **Offline storage**: Limited only by device storage

## Support and Resources

### Official Documentation
- [Meshtastic Documentation](https://meshtastic.org/docs/)
- [ATAK COT Spec](https://www.mitre.org/sites/default/files/pdf/09_4937.pdf)
- [Web Bluetooth API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Bluetooth_API)

### Common Issues
See troubleshooting section above or file an issue on GitHub.

### Contributing
Contributions welcome! See the main project README for guidelines.

---

**Note**: This integration is designed for tactical operations and emergency communication. Always ensure compliance with local radio regulations and obtain necessary licenses for LoRa operation.
