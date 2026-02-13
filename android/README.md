# LPU5 Tactical - Android Native Application

## Overview

Native Android application for LPU5 Tactical with deep Meshtastic mesh communication integration. This app wraps the web-based overview.html in a WebView while providing native BLE/Serial access to Meshtastic devices, GPS tracking, and full offline mesh networking capabilities.

## Features

### Native Capabilities
- ✅ **Meshtastic SDK Integration** - Direct BLE and serial connection to Meshtastic devices
- ✅ **GPS Position Tracking** - Native location services with background updates
- ✅ **WebView Bridge** - Seamless JavaScript ↔ Native communication
- ✅ **Offline Operation** - Full mesh networking without internet
- ✅ **COT Message Exchange** - Native COT protocol support
- ✅ **Background Services** - Meshtastic service runs in background
- ✅ **Battery Optimized** - Efficient BLE and GPS usage

### WebView Features
- Same UI as web version (overview.html)
- Interactive map with Leaflet.js
- Real-time mesh node visualization
- Chat interface for mesh messages
- COT message display
- Marker management

## Requirements

### Development
- **Android Studio**: Arctic Fox (2020.3.1) or newer
- **JDK**: 11 or newer
- **Android SDK**: API 24+ (Android 7.0+)
- **Gradle**: 8.0+ (included via wrapper)

### Target Devices
- **Android Version**: 7.0+ (API 24+)
- **Bluetooth**: BLE (Bluetooth Low Energy) required
- **GPS**: Optional but recommended
- **RAM**: 2GB+ recommended
- **Storage**: 50MB+ free space

## Project Structure

```
android/
├── app/
│   ├── build.gradle                 # App-level build configuration
│   ├── proguard-rules.pro           # ProGuard rules for release
│   └── src/
│       └── main/
│           ├── AndroidManifest.xml  # App manifest with permissions
│           ├── java/com/lpu5/tactical/
│           │   └── MainActivity.kt   # Main activity with WebView
│           ├── res/
│           │   ├── layout/
│           │   │   └── activity_main.xml  # Main layout
│           │   ├── values/
│           │   │   ├── strings.xml   # String resources
│           │   │   └── themes.xml    # App theme
│           │   └── ...
│           └── assets/
│               └── www/
│                   ├── overview.html  # Main web UI
│                   ├── meshtastic-web-client.js
│                   ├── cot-client.js
│                   └── message-queue-manager.js
├── build.gradle                     # Project-level build configuration
├── settings.gradle                  # Project settings
└── gradle/
    └── wrapper/                     # Gradle wrapper
```

## Building the App

### 1. Clone Repository
```bash
git clone https://github.com/fredy060795/lpu5-tactical.git
cd lpu5-tactical/android
```

### 2. Open in Android Studio
- Launch Android Studio
- Select "Open an Existing Project"
- Navigate to `lpu5-tactical/android`
- Wait for Gradle sync to complete

### 3. Build Debug APK
```bash
# Using Gradle wrapper (recommended)
./gradlew assembleDebug

# Output: app/build/outputs/apk/debug/app-debug.apk
```

### 4. Build Release APK
```bash
# First, create a keystore for signing
keytool -genkey -v -keystore lpu5-tactical.keystore \
  -alias lpu5-key -keyalg RSA -keysize 2048 -validity 10000

# Configure signing in app/build.gradle (see below)

# Build release APK
./gradlew assembleRelease

# Output: app/build/outputs/apk/release/app-release.apk
```

### Configuring Release Signing

Edit `app/build.gradle` to add signing configuration:

```gradle
android {
    signingConfigs {
        release {
            storeFile file("../lpu5-tactical.keystore")
            storePassword "your-secure-password"
            keyAlias "lpu5-key"
            keyPassword "your-secure-password"
        }
    }
    
    buildTypes {
        release {
            signingConfig signingConfigs.release
            minifyEnabled true
            proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro'
        }
    }
}
```

**Security Note**: Never commit keystore passwords to version control. Use environment variables or gradle.properties with .gitignore.

## Installation

### Install via ADB
```bash
# Connect Android device via USB
# Enable USB debugging in Developer Options

adb install app/build/outputs/apk/debug/app-debug.apk
```

### Install via File Transfer
1. Copy APK to device (email, cloud, USB)
2. Open APK file on device
3. Allow "Install from unknown sources" if prompted
4. Tap "Install"

### Install from Play Store (Production)
1. Create Google Play Developer account ($25 one-time fee)
2. Prepare app listing (screenshots, description, etc.)
3. Upload signed release APK or App Bundle
4. Complete content rating and privacy policy
5. Submit for review (1-3 days)

## Configuration

### Permissions
The app requests the following runtime permissions:

- **Location** (Fine & Coarse)
  - Required for GPS position tracking
  - Used for location-based features
  
- **Bluetooth** (Connect & Scan)
  - Required for Meshtastic BLE connection
  - Used for mesh communication
  
- **Nearby Devices** (Android 12+)
  - Required for BLE device discovery
  - Used for finding Meshtastic devices

### Meshtastic Device Configuration
1. Flash Meshtastic firmware 2.0+ to your device
2. Configure channel and encryption via Meshtastic app
3. Enable Bluetooth in device settings
4. Note your device name for pairing

## Usage

### First Launch
1. Grant all requested permissions
2. App loads with map interface
3. Tap Meshtastic icon (bottom toolbar)
4. Tap "Connect Device"
5. Select your Meshtastic device
6. Wait for connection (green indicator)

### Sending Messages
```
Text Message:
1. Open Meshtastic panel
2. Type message in text field
3. Uncheck "Send as COT"
4. Tap "Send"

COT Message:
1. Open Meshtastic panel
2. Type description
3. Check "Send as COT"
4. Tap "Send"
5. Your GPS position is included
```

### Viewing Mesh Network
- Connected nodes appear as blue markers on map
- Tap marker to see node details (name, position, last seen)
- Node list shown in Meshtastic panel

### Background Operation
- App continues tracking position in background
- Meshtastic service remains connected
- Notifications for incoming messages (future feature)
- Battery optimization recommended: disable for this app

## JavaScript Bridge API

The app provides a native bridge for enhanced functionality:

### Available Functions

```javascript
// Check if running in Android app
if (window.isAndroidNative) {
    console.log('Running in native Android app');
}

// Connect to Meshtastic device
window.nativeConnectMeshtastic();

// Send message via mesh
window.nativeSendMessage("Hello mesh!", false);  // Text message
window.nativeSendMessage("<event>...</event>", true);  // COT message

// Get current GPS position
const position = JSON.parse(window.nativeGetPosition());
console.log(position.latitude, position.longitude);

// Get mesh nodes
const nodes = JSON.parse(window.nativeGetMeshtasticNodes());

// Show native toast notification
Android.showToast("Custom message");
```

### Event Listeners

```javascript
// Listen for native events
window.onAndroidEvent = function(event, data) {
    const eventData = JSON.parse(data);
    
    switch(event) {
        case 'locationUpdate':
            console.log('Position:', eventData.latitude, eventData.longitude);
            break;
            
        case 'messageSent':
            console.log('Message sent:', eventData.message);
            break;
            
        case 'meshtasticServiceConnected':
            console.log('Meshtastic ready');
            break;
            
        case 'meshtasticServiceDisconnected':
            console.log('Meshtastic disconnected');
            break;
    }
};
```

## Dependencies

### Main Dependencies
```gradle
// AndroidX Core
androidx.core:core-ktx:1.12.0
androidx.appcompat:appcompat:1.6.1
com.google.android.material:material:1.11.0

// WebView
androidx.webkit:webkit:1.9.0

// Meshtastic SDK
com.geeksville.mesh:meshtastic-android:2.3.2

// Location Services
com.google.android.gms:play-services-location:21.0.1

// JSON
com.google.code.gson:gson:2.10.1

// Coroutines
org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3
```

## Troubleshooting

### Build Issues

**Gradle sync failed**
```bash
# Clean and rebuild
./gradlew clean
./gradlew build
```

**Dependency resolution errors**
- Check internet connection
- Update Android Studio
- Sync Gradle files
- Invalidate caches: File → Invalidate Caches / Restart

### Runtime Issues

**"Meshtastic service not connected"**
- Ensure Bluetooth is enabled
- Grant all permissions
- Restart app
- Check Meshtastic device is powered on

**"Location not updating"**
- Grant location permissions
- Enable high-accuracy mode
- Disable battery optimization
- Check GPS signal (go outside)

**"Cannot send messages"**
- Verify Meshtastic connection
- Check device is on same channel
- Ensure LoRa coverage
- Try reconnecting device

### Performance Issues

**High battery drain**
- Reduce GPS update frequency
- Disconnect when not in use
- Disable background location if not needed
- Check for wakelocks in battery settings

**Slow WebView performance**
- Clear WebView cache: Settings → Apps → LPU5 Tactical → Storage → Clear cache
- Reduce map zoom/complexity
- Limit number of markers displayed

## Development

### Running from Android Studio
1. Connect device or start emulator
2. Click "Run" (green play button) or Shift+F10
3. Select target device
4. App installs and launches automatically

### Debugging
```bash
# View real-time logs
adb logcat | grep LPU5

# View WebView console logs
adb logcat | grep chromium

# View crash logs
adb logcat | grep AndroidRuntime
```

### WebView Debugging
1. Enable USB debugging on device
2. Connect to computer
3. Open Chrome on computer
4. Navigate to chrome://inspect
5. Click "inspect" under your device
6. Use Chrome DevTools to debug WebView

## Testing

### Unit Tests
```bash
./gradlew test
```

### Instrumented Tests
```bash
./gradlew connectedAndroidTest
```

### Manual Testing Checklist
- [ ] App installs successfully
- [ ] Permissions requested and granted
- [ ] Meshtastic device connects via BLE
- [ ] GPS position updates
- [ ] Text messages send successfully
- [ ] COT messages send with position
- [ ] Map displays nodes
- [ ] Offline operation works
- [ ] Background service runs
- [ ] App survives rotation
- [ ] No crashes or ANRs

## Release Checklist

Before releasing to production:

- [ ] Increment versionCode and versionName
- [ ] Test on multiple devices/Android versions
- [ ] Enable ProGuard/R8 obfuscation
- [ ] Sign with release keystore
- [ ] Test release build thoroughly
- [ ] Prepare Play Store assets:
  - [ ] Icon (512x512)
  - [ ] Feature graphic (1024x500)
  - [ ] Screenshots (phone, tablet)
  - [ ] App description
  - [ ] Privacy policy URL
- [ ] Complete content rating questionnaire
- [ ] Upload to Play Store
- [ ] Submit for review

## License

[Specify license here]

## Support

- **Issues**: [GitHub Issues](https://github.com/fredy060795/lpu5-tactical/issues)
- **Documentation**: [Main README](../README.md)
- **Deployment**: [DEPLOYMENT.md](../DEPLOYMENT.md)
- **Architecture**: [MULTI_PLATFORM_ARCHITECTURE.md](../MULTI_PLATFORM_ARCHITECTURE.md)

## Acknowledgments

- Meshtastic Project: https://meshtastic.org/
- Android Open Source Project
- Contributors and testers

---

**For iOS PWA version, see**: [../pwa/README.md](../pwa/README.md)
