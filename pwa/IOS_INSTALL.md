# iOS Installation Guide - LPU5 Tactical PWA

## Quick Start for iOS Users

This guide will walk you through installing the LPU5 Tactical Progressive Web App (PWA) on your iOS device (iPhone or iPad).

---

## Prerequisites

Before starting, ensure you have:

- ✅ **iOS Device** running iOS 11.3 or later
- ✅ **Safari Browser** (default iOS browser)
- ✅ **Internet Connection** (WiFi or cellular)
- ✅ **HQ Server URL** provided by your administrator (e.g., `https://192.168.1.100:8001`)

⚠️ **Important**: You must use Safari. Other browsers (Chrome, Firefox) do not support PWA installation on iOS.

---

## Installation Steps

### Step 1: Open Safari

1. Locate the **Safari** app on your home screen
2. Tap to open Safari browser

### Step 2: Navigate to the PWA URL

1. Tap the **address bar** at the top
2. Enter the URL provided by your administrator:
   ```
   https://[HQ-SERVER-IP]:8001/pwa/overview.html
   ```
   **Example**: `https://192.168.1.100:8001/pwa/overview.html`

3. Tap **Go** or press Enter

### Step 3: Accept SSL Certificate (If Prompted)

If your HQ is using a self-signed certificate, you may see a security warning:

1. Tap **Show Details** or **Advanced**
2. Tap **Visit this website** or **Proceed anyway**
3. Confirm that you trust this connection

> **Note**: This is normal for internal servers with self-signed certificates. Always verify you're connecting to the correct server IP address.

### Step 4: Test the Application

Before installing, make sure the page loads correctly:

1. You should see the tactical map interface
2. The login screen should appear
3. Test logging in with your credentials (optional)

### Step 5: Add to Home Screen

This is the key step that installs the PWA:

1. Tap the **Share** button at the bottom of Safari
   - It looks like a square with an arrow pointing up ⬆️
   
2. In the share menu that appears, scroll down to find options
   
3. Tap **"Add to Home Screen"**
   - Look for the icon with a plus (+) sign inside a square
   - You may need to scroll down in the menu to find it

### Step 6: Customize Installation

1. A preview screen appears showing the app icon and name
2. **Edit the name** if desired (default: "LPU5 Tactical")
   - Tap on the name field to edit
   - Recommended: Keep it short (e.g., "LPU5" or "Tactical")
3. Tap **"Add"** in the top-right corner

### Step 7: Launch the App

1. Return to your home screen
2. Find the new **LPU5 Tactical** icon
3. Tap the icon to launch the app
4. The app will open in **standalone mode** (no Safari UI visible)

✅ **Installation Complete!** The app now works like a native iOS app.

---

## First Use

### Initial Login

1. When you first open the app, you'll see the login screen
2. Enter your **username** and **password**
3. Tap **Login**
4. Your session will be saved for future use

### Granting Permissions

The app may request permissions:

- **Location Services**: Allows the app to show your position on the map
  - Tap **Allow While Using App** (recommended)
  - Or **Allow Once** for temporary access

---

## Using the PWA

### Opening the App

- **From Home Screen**: Tap the LPU5 Tactical icon
- **Switching Apps**: Use standard iOS app switcher (swipe up)
- **Closing**: Swipe up from bottom and swipe up on the app preview

### Key Features

✅ **Works Offline**: After first load, basic functionality works without internet
✅ **Home Screen Icon**: Launches like a native app
✅ **Standalone Display**: No Safari browser UI
✅ **Auto-Updates**: Automatically updates when connected to HQ
✅ **Real-time**: Live map updates via WebSocket when online

### iOS PWA Limitations

Due to iOS Safari restrictions, the following are NOT available:

❌ **No Direct Bluetooth**: Cannot connect directly to Meshtastic devices
❌ **Limited Background**: App pauses when not in foreground
❌ **No Push Notifications**: Cannot receive background notifications
❌ **Requires Internet**: Must connect to HQ server (no offline mesh)

> **Workaround**: All mesh communication goes through HQ's gateway. When HQ has a Meshtastic device connected, iOS devices receive mesh updates through the server.

---

## Communication Modes

### iOS PWA Communication Architecture

```
iOS Device (PWA)
     ↕ HTTPS/WebSocket
HQ Server (Gateway)
     ↕ BLE/Serial
Meshtastic Device
     ↕ LoRa Mesh
Field Network
```

### How It Works

1. **You (iOS)** → Send marker/message via REST API → **HQ Server**
2. **HQ Server** → Forward to Meshtastic Gateway → **Mesh Network**
3. **Mesh Network** → Receive updates → **HQ Server** → **You (iOS)**

### Online vs Offline Behavior

| Feature | Online (Connected to HQ) | Offline |
|---------|-------------------------|---------|
| View Map | ✅ Yes | ✅ Yes (cached) |
| Create Markers | ✅ Yes | ⚠️ Queued |
| Real-time Updates | ✅ Yes | ❌ No |
| Mesh Messages | ✅ Via HQ Gateway | ❌ No |
| Login | ✅ Yes | ❌ No |
| GPS Tracking | ✅ Yes | ⚠️ Limited |

---

## Troubleshooting

### Problem: "Add to Home Screen" Option Missing

**Causes:**
- Not using Safari browser
- Using Private Browsing mode
- Already installed
- iOS version too old

**Solutions:**
1. Make sure you're using **Safari** (not Chrome or other browser)
2. Exit **Private Browsing**: Tap tabs icon, tap "Private", tap "Done"
3. If already installed, remove the old app first
4. Update iOS: Settings → General → Software Update

---

### Problem: SSL Certificate Warning Won't Go Away

**Causes:**
- Invalid SSL certificate
- Incorrect server URL
- Network issues

**Solutions:**
1. Verify server URL is correct
2. Ask HQ admin to verify certificate is properly installed
3. Try accessing from a computer first to verify certificate
4. Check if your device time/date is correct: Settings → General → Date & Time

---

### Problem: App Won't Load After Installation

**Causes:**
- No internet connection
- Server is offline
- Cache corruption

**Solutions:**
1. Check WiFi/cellular connection
2. Try opening in Safari first: `https://[HQ-IP]:8001/pwa/overview.html`
3. Delete and reinstall the app
4. Clear Safari cache: Settings → Safari → Clear History and Website Data

---

### Problem: Login Fails or Session Expires

**Causes:**
- Incorrect credentials
- Server authentication issues
- Token expired

**Solutions:**
1. Verify username and password
2. Check with HQ admin that your account is active
3. Clear cache and try again
4. Contact HQ to reset your password

---

### Problem: No Real-time Updates

**Causes:**
- WebSocket connection failed
- Server not broadcasting
- App in background

**Solutions:**
1. Keep app in **foreground** (iOS limits background web apps)
2. Pull down to refresh the map
3. Check internet connection
4. Verify HQ server is running
5. Ask HQ admin to check WebSocket service

---

### Problem: Location Not Working

**Causes:**
- Location permission denied
- Location services disabled
- GPS signal weak

**Solutions:**
1. Grant location permission: Settings → LPU5 Tactical → Location
2. Enable Location Services: Settings → Privacy → Location Services (ON)
3. Go outside for better GPS signal
4. Restart the app

---

## Advanced Tips

### Offline Usage

To maximize offline capability:

1. **Pre-cache content**: Open all sections while connected
2. **Download map area**: Zoom/pan around your operation area
3. **Stay logged in**: Don't log out before going offline
4. **Reload periodically**: Refresh when connection available

### Battery Optimization

To preserve battery life:

1. **Reduce GPS updates**: Set longer update intervals
2. **Minimize map interactions**: Don't constantly zoom/pan
3. **Close when not needed**: Don't leave running in background
4. **Use Low Power Mode**: iOS Settings → Battery → Low Power Mode

### Multiple Devices

You can install the PWA on multiple iOS devices:

- Same login credentials work on all devices
- Each device syncs through HQ server
- Updates appear on all connected devices
- Each installation is independent

### Updating the App

The PWA updates automatically:

1. Updates download in background when connected
2. Refresh the app to apply updates (pull down on map)
3. No manual update process needed
4. HQ admin controls when updates are released

---

## Security Best Practices

### Secure Your Device

1. **Enable Passcode**: Settings → Face ID & Passcode
2. **Auto-Lock**: Settings → Display & Brightness → Auto-Lock (set to 1-5 min)
3. **Don't Share Credentials**: Keep your login information private
4. **Log Out**: When using shared devices, always log out

### Network Security

1. **Use Secure WiFi**: Avoid public WiFi for sensitive operations
2. **Verify Server URL**: Always check you're connecting to correct IP
3. **Trust HQ Certificate**: Only accept certificate from your HQ
4. **Report Issues**: Contact HQ immediately if something seems wrong

### Data Protection

1. **No Screenshots**: Don't take screenshots of sensitive information
2. **Screen Privacy**: Be aware of shoulder surfing
3. **Lost Device**: Report immediately and have HQ disable your account
4. **Regular Logout**: Log out when done with session

---

## Comparison: iOS PWA vs Android Native

| Feature | iOS PWA | Android Native App |
|---------|---------|-------------------|
| **Installation** | Add to Home Screen | APK Install |
| **Bluetooth** | ❌ No (Safari limit) | ✅ Yes (Native) |
| **GPS Tracking** | ⚠️ Browser API | ✅ Native API |
| **Offline Mesh** | ❌ Requires HQ | ✅ Full Offline |
| **Internet** | ✅ Required | ❌ Optional |
| **Background** | ⚠️ Limited | ✅ Full Support |
| **Updates** | 🔄 Automatic | 📦 Manual/Store |
| **Size** | ~500KB cache | ~15MB APK |
| **Distribution** | 🌐 Web URL | 📱 APK/Play Store |

### When to Use iOS PWA

✅ **Ideal for:**
- HQ personnel and commanders
- Remote coordination with reliable internet
- Quick deployment without app store
- Viewing mesh network status
- Non-critical operations

❌ **Not Ideal for:**
- Field operations without internet
- Direct mesh communication required
- Critical real-time updates needed
- Offline-first requirements

> **Recommendation**: For field operators needing direct mesh access, use Android devices with the native app instead.

---

## Support & Resources

### Getting Help

- **HQ Admin**: Your first point of contact for access issues
- **Technical Support**: [Repository Issues](https://github.com/fredy060795/lpu5-tactical/issues)
- **Documentation**: See [pwa/README.md](README.md) for detailed technical documentation

### Additional Documentation

- **[README.md](README.md)** - Detailed PWA technical documentation
- **[../DEPLOYMENT.md](../DEPLOYMENT.md)** - Server deployment guide
- **[../README.md](../README.md)** - Main project overview
- **[../android/README.md](../android/README.md)** - Android native app documentation

### Useful Commands (For HQ Admins)

```bash
# Check if server is accessible
curl -k https://[HQ-IP]:8001/api/status

# View server logs
tail -f /var/log/lpu5-tactical.log

# Restart server
systemctl restart lpu5-tactical
```

---

## FAQ

**Q: Can I use Chrome or Firefox on iOS?**  
A: No. Only Safari supports PWA installation on iOS. Other browsers cannot add web apps to the home screen.

**Q: Will this use up my data plan?**  
A: Initial installation downloads ~500KB. After that, only real-time updates use data (minimal). Use WiFi when possible.

**Q: Can I use this on iPad?**  
A: Yes! The same installation process works on iPad. The interface adapts to the larger screen.

**Q: Do I need to be connected to HQ all the time?**  
A: No, basic map viewing works offline. However, real-time updates and new markers require connection to HQ.

**Q: How do I uninstall the PWA?**  
A: Long-press the app icon → tap "Remove App" → tap "Delete App". You can reinstall anytime.

**Q: Can multiple people share one device?**  
A: Not recommended. Each person should use their own device with their own credentials. If sharing is necessary, always log out between users.

**Q: What happens if HQ server goes down?**  
A: You can still view cached map data, but no real-time updates. New actions will be queued until connection is restored.

**Q: Is my location data tracked?**  
A: Location is only used to display your position on the map. It's sent to HQ server when you're online. Check your organization's privacy policy for details.

---

## Quick Reference

### Essential URLs

```
Main App:     https://[HQ-IP]:8001/pwa/overview.html
API Status:   https://[HQ-IP]:8001/api/status
Admin Panel:  https://[HQ-IP]:8001/admin.html
```

### Key Contacts

- **HQ Administrator**: _______________
- **Technical Support**: _______________
- **Emergency Contact**: _______________

### Version Information

- **Current Version**: 3.0 (iOS PWA)
- **Last Updated**: 2024
- **Compatibility**: iOS 11.3+

---

**For Android Users**: See [../android/README.md](../android/README.md) for native app installation.

**For System Administrators**: See [../DEPLOYMENT.md](../DEPLOYMENT.md) for server setup.
