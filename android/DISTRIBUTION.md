# Android APK Distribution Guide - LPU5 Tactical

## Overview

This guide covers how to build, sign, distribute, and install the LPU5 Tactical Android native application. This is intended for system administrators and technical personnel responsible for deploying the app to field devices.

---

## Table of Contents

1. [Building the APK](#building-the-apk)
2. [Signing for Release](#signing-for-release)
3. [Distribution Methods](#distribution-methods)
4. [Installation on Devices](#installation-on-devices)
5. [Google Play Store Deployment](#google-play-store-deployment)
6. [Security Considerations](#security-considerations)
7. [Version Management](#version-management)
8. [Troubleshooting](#troubleshooting)

---

## Building the APK

### Prerequisites

Before building, ensure you have:

- ✅ **Android Studio** Arctic Fox (2020.3.1) or newer
- ✅ **JDK 11** or newer
- ✅ **Android SDK** with API 24+ (Android 7.0+)
- ✅ **Gradle 8.0+** (included with Android Studio)
- ✅ **Git** for version control

### Initial Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/fredy060795/lpu5-tactical.git
   cd lpu5-tactical/android
   ```

2. **Open in Android Studio**:
   - Launch Android Studio
   - File → Open
   - Navigate to `lpu5-tactical/android`
   - Click OK

3. **Wait for Gradle Sync**:
   - Android Studio will automatically sync Gradle
   - Download dependencies (may take several minutes)
   - Fix any SDK version prompts if needed

### Build Debug APK (Testing)

Debug builds are for **testing only** - not for production deployment.

**Via Android Studio**:
1. Build → Build Bundle(s) / APK(s) → Build APK(s)
2. Wait for build to complete
3. Click "locate" in notification to find APK

**Via Command Line**:
```bash
cd android
./gradlew assembleDebug
```

**Output Location**:
```
android/app/build/outputs/apk/debug/app-debug.apk
```

**Characteristics**:
- ⚠️ Not signed for production
- ⚠️ Includes debugging symbols (larger size)
- ⚠️ Not optimized
- ✅ Useful for testing
- ✅ Can be installed via ADB easily

---

## Signing for Release

Production APKs **must be signed** with a release keystore.

### Create Keystore (One-time)

```bash
keytool -genkey -v -keystore lpu5-tactical.keystore \
  -alias lpu5-key \
  -keyalg RSA \
  -keysize 2048 \
  -validity 10000
```

**Interactive Prompts**:
```
Enter keystore password: [CREATE-STRONG-PASSWORD]
Re-enter password: [REPEAT-PASSWORD]
What is your first and last name?: [Organization Name]
What is your name of your organizational unit?: [Department]
What is the name of your organization?: [Company]
What is the name of your City or Locality?: [City]
What is the name of your State or Province?: [State]
What is the two-letter country code?: [US]
Is this correct? yes

Enter key password for <lpu5-key>: [KEY-PASSWORD]
Re-enter: [REPEAT-KEY-PASSWORD]
```

**⚠️ CRITICAL**: 
- **Backup this keystore** in a secure location
- **Store passwords** in a password manager
- **Never commit** keystore to version control
- **Losing this keystore** means you cannot update the app

### Configure Signing in Gradle

**Option 1: Environment Variables (Recommended)**

1. Create `android/keystore.properties`:
   ```properties
   storeFile=../lpu5-tactical.keystore
   storePassword=YOUR_STORE_PASSWORD
   keyAlias=lpu5-key
   keyPassword=YOUR_KEY_PASSWORD
   ```

2. Add to `.gitignore`:
   ```bash
   echo "android/keystore.properties" >> .gitignore
   ```

3. Update `android/app/build.gradle`:
   ```gradle
   // Load keystore properties
   def keystorePropertiesFile = rootProject.file("keystore.properties")
   def keystoreProperties = new Properties()
   if (keystorePropertiesFile.exists()) {
       keystoreProperties.load(new FileInputStream(keystorePropertiesFile))
   }

   android {
       ...
       
       signingConfigs {
           release {
               if (keystorePropertiesFile.exists()) {
                   storeFile file(keystoreProperties['storeFile'])
                   storePassword keystoreProperties['storePassword']
                   keyAlias keystoreProperties['keyAlias']
                   keyPassword keystoreProperties['keyPassword']
               }
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

**Option 2: Manual Signing After Build**

Build unsigned APK, then sign manually:
```bash
# Build unsigned release
./gradlew assembleRelease

# Sign with jarsigner
jarsigner -verbose -sigalg SHA256withRSA -digestalg SHA-256 \
  -keystore lpu5-tactical.keystore \
  app/build/outputs/apk/release/app-release-unsigned.apk \
  lpu5-key

# Align APK
zipalign -v 4 app/build/outputs/apk/release/app-release-unsigned.apk \
  app/build/outputs/apk/release/app-release.apk
```

### Build Release APK

**Via Android Studio**:
1. Build → Generate Signed Bundle / APK
2. Select APK → Next
3. Select keystore → Enter passwords → Next
4. Select "release" build variant → Finish

**Via Command Line**:
```bash
cd android
./gradlew assembleRelease
```

**Output Location**:
```
android/app/build/outputs/apk/release/app-release.apk
```

**Verify Signing**:
```bash
jarsigner -verify -verbose -certs app/build/outputs/apk/release/app-release.apk
```

Should show: `jar verified.`

---

## Distribution Methods

### Method 1: Direct File Transfer (Simple)

**Best for**: Small teams, field deployment, testing

**Steps**:
1. Copy `app-release.apk` to a USB drive
2. Transfer to Android device
3. Open file manager on device
4. Tap APK file → Install

**Pros**: Simple, no internet required  
**Cons**: Manual process, no auto-updates

---

### Method 2: Web Download (Scalable)

**Best for**: Larger teams, remote users

**Setup**:
1. Host APK on web server:
   ```bash
   # Copy to web server
   cp app-release.apk /var/www/html/lpu5-tactical.apk
   
   # Or use Python simple server
   cd android/app/build/outputs/apk/release
   python3 -m http.server 8080
   ```

2. Share URL with users:
   ```
   http://your-server.com/lpu5-tactical.apk
   ```

**User Instructions**:
1. Open URL in mobile browser
2. Download APK
3. Tap downloaded file → Install
4. Allow "Install from unknown sources" if prompted

**Pros**: Easy distribution, accessible anywhere  
**Cons**: Requires internet, users must enable unknown sources

---

### Method 3: ADB Installation (IT Admin)

**Best for**: IT departments, bulk deployment

**Prerequisites**:
- ADB (Android Debug Bridge) installed
- USB debugging enabled on devices
- USB connection to each device

**Steps**:
```bash
# Connect device via USB
adb devices

# Install APK
adb install app-release.apk

# Or force reinstall
adb install -r app-release.apk

# Install on multiple devices
for device in $(adb devices | grep device | awk '{print $1}'); do
    adb -s $device install app-release.apk
done
```

**Pros**: Reliable, can automate  
**Cons**: Requires USB connection, technical knowledge

---

### Method 4: Mobile Device Management (MDM)

**Best for**: Enterprise deployments

**Supported MDM Solutions**:
- Google Workspace (G Suite)
- Microsoft Intune
- VMware Workspace ONE
- IBM MaaS360
- Jamf

**Process**:
1. Upload APK to MDM console
2. Create deployment policy
3. Assign to device groups
4. Push to devices

**Pros**: Centralized management, automatic updates, policy enforcement  
**Cons**: Requires MDM infrastructure, licensing costs

---

### Method 5: QR Code Distribution

**Best for**: Field deployment, training

**Generate QR Code**:
```bash
# Install qrencode
sudo apt-get install qrencode

# Generate QR code for download URL
qrencode -o lpu5-qr.png "http://your-server.com/lpu5-tactical.apk"
```

**User Instructions**:
1. Open camera or QR scanner app
2. Scan QR code
3. Tap link to download
4. Install APK

**Pros**: Quick deployment, no typing URLs  
**Cons**: Still requires enabling unknown sources

---

## Installation on Devices

### Prerequisites on Device

1. **Enable Unknown Sources**:
   - Android 8.0+: Settings → Apps → Special app access → Install unknown apps → [Your Browser] → Allow
   - Android 7.x: Settings → Security → Unknown sources → Enable

2. **Sufficient Storage**:
   - Minimum 50MB free space
   - Recommended 100MB for updates

3. **Android Version**:
   - Minimum: Android 7.0 (API 24)
   - Recommended: Android 9.0+

### Installation Steps (End User)

1. **Download/Transfer APK** (via one of the methods above)

2. **Tap APK file** in file manager or downloads

3. **Review Permissions**:
   - Location (for GPS)
   - Bluetooth (for Meshtastic)
   - Nearby devices (Android 12+)

4. **Tap "Install"**

5. **Wait for installation** (5-10 seconds)

6. **Tap "Open"** or find app in app drawer

### First Launch

1. Grant all requested permissions
2. App connects to Meshtastic service
3. Main map interface loads
4. Follow on-screen instructions

---

## Google Play Store Deployment

### Prerequisites

- Google Play Developer account ($25 one-time fee)
- Signed release APK or AAB (Android App Bundle)
- App graphics (icon, screenshots, feature graphic)
- Privacy policy URL
- Content rating

### Prepare App Bundle (Recommended)

Google Play prefers AAB format over APK:

```bash
cd android
./gradlew bundleRelease
```

Output: `app/build/outputs/bundle/release/app-release.aab`

### Create Play Console Listing

1. **Go to**: [Google Play Console](https://play.google.com/console)

2. **Create Application**:
   - All apps → Create app
   - Name: "LPU5 Tactical - Meshtastic Network"
   - Language: English (or primary language)
   - App or Game: App
   - Free or Paid: Free (or Paid)

3. **Store Listing**:
   - **App name**: LPU5 Tactical
   - **Short description** (80 chars):
     ```
     Tactical network management with Meshtastic mesh communication for field ops
     ```
   - **Full description** (4000 chars):
     ```
     LPU5 Tactical is a comprehensive tactical tracking and communication 
     system designed for field operations with integrated Meshtastic mesh 
     networking capabilities.
     
     KEY FEATURES:
     • Native Meshtastic BLE/Serial integration for direct mesh communication
     • Real-time tactical map with GPS position tracking
     • COT (Cursor on Target) protocol support for ATAK compatibility
     • Full offline mesh networking (no internet required)
     • Background location updates and service
     • Secure authentication and role-based access
     • Interactive mapping with markers, drawings, and overlays
     
     [Continue with more details...]
     ```

4. **Graphics**:
   - **Icon**: 512×512 PNG (32-bit, no transparency)
   - **Feature graphic**: 1024×500 PNG
   - **Screenshots**: 
     - Phone: Minimum 2, at least 320×320, up to 3840×3840
     - Tablet (optional): Same as phone
   - **Promo video** (optional): YouTube URL

5. **Categorization**:
   - **App category**: Business or Productivity
   - **Tags**: tactical, communication, mesh, meshtastic, field ops

6. **Contact Details**:
   - Email address
   - Phone number (optional)
   - Website (optional)

7. **Privacy Policy**:
   - Must provide URL to privacy policy
   - Host on accessible website

### App Content

1. **Privacy & Security**:
   - Does your app access location? → Yes
   - Is your app primarily for children? → No
   - Does your app contain ads? → No (typically)

2. **Content Rating**:
   - Complete questionnaire
   - Based on content, likely "Everyone" or "Teen"

3. **Target Audience**:
   - Age range: 18+ or appropriate range

4. **Data Safety**:
   - Declare what data is collected
   - Location: Collected and shared (for mapping)
   - User credentials: Collected (for authentication)
   - Device ID: Collected (optional)

### App Access

- If app requires login:
  - Provide test credentials
  - Explain any special requirements

### App Release

1. **Production Track** (or Internal/Beta first):
   - Upload signed AAB/APK
   - Choose countries/regions
   - Set rollout percentage (start with 20%, then 100%)

2. **Review**:
   - Confirm all sections complete
   - Submit for review

3. **Wait for Approval**:
   - Typically 1-3 days
   - May be asked for clarifications

4. **Publish**:
   - Once approved, app goes live
   - Users can find and install from Play Store

### Update Process

1. Increment `versionCode` and `versionName` in `app/build.gradle`
2. Build new signed AAB/APK
3. Upload to same app listing
4. Add release notes
5. Submit for review

---

## Security Considerations

### APK Security

1. **Always Sign Release Builds**:
   - Never distribute unsigned APKs
   - Use strong keystore password
   - Keep keystore secure and backed up

2. **Enable ProGuard/R8**:
   ```gradle
   buildTypes {
       release {
           minifyEnabled true
           shrinkResources true
           proguardFiles getDefaultProguardFile('proguard-android-optimize.txt')
       }
   }
   ```

3. **Remove Debug Logs**:
   - Strip debug logging in release builds
   - Remove sensitive information from logs

### Distribution Security

1. **Verify APK Integrity**:
   ```bash
   # Generate SHA-256 checksum
   sha256sum app-release.apk
   ```
   - Share checksum with users
   - Users verify after download

2. **Use HTTPS**:
   - Always host APK on HTTPS server
   - Prevents man-in-the-middle attacks

3. **Access Control**:
   - Restrict download URL access if possible
   - Use authentication for downloads
   - Log download attempts

### Device Security

1. **Educate Users**:
   - Only install from trusted sources
   - Verify app signature
   - Don't share APK publicly

2. **App Permissions**:
   - Request only necessary permissions
   - Explain why each permission is needed
   - Allow users to deny non-critical permissions

3. **Data Protection**:
   - Encrypt sensitive data at rest
   - Use HTTPS for network communication
   - Clear credentials on logout

---

## Version Management

### Version Numbering

Follow semantic versioning in `app/build.gradle`:

```gradle
android {
    defaultConfig {
        versionCode 1      // Integer, increment for each release
        versionName "1.0.0" // String, displayed to users
    }
}
```

**Versioning Scheme**:
- **Major.Minor.Patch** (e.g., 2.3.1)
- **Major**: Breaking changes, major features
- **Minor**: New features, backward compatible
- **Patch**: Bug fixes, minor changes

**versionCode Rules**:
- Must be higher than previous release
- Integer only, typically 1, 2, 3, ...
- Google Play requires higher versionCode for updates

### Changelog

Maintain `CHANGELOG.md`:

```markdown
# Changelog

## [1.1.0] - 2024-02-13
### Added
- New COT message filtering
- Background location tracking

### Fixed
- Bluetooth connection timeout
- GPS accuracy improvements

### Changed
- Updated Meshtastic SDK to 2.3.2
```

### Release Checklist

Before each release:

- [ ] Increment versionCode and versionName
- [ ] Update CHANGELOG.md
- [ ] Test on multiple devices/Android versions
- [ ] Run all unit and integration tests
- [ ] Verify ProGuard/R8 doesn't break functionality
- [ ] Sign with release keystore
- [ ] Generate SHA-256 checksum
- [ ] Test installation from APK
- [ ] Create Git tag: `git tag v1.1.0`
- [ ] Push tag: `git push origin v1.1.0`

---

## Troubleshooting

### Build Issues

**Problem**: `Gradle sync failed`

**Solutions**:
```bash
# Clean and rebuild
./gradlew clean
./gradlew build

# Update Gradle wrapper
./gradlew wrapper --gradle-version 8.2

# Clear Gradle cache
rm -rf ~/.gradle/caches/
```

---

**Problem**: `Unable to find Meshtastic SDK dependency`

**Solution**:
Add Maven repository in `build.gradle`:
```gradle
repositories {
    google()
    mavenCentral()
    maven { url 'https://jitpack.io' }
}
```

---

### Signing Issues

**Problem**: `Failed to sign APK`

**Solutions**:
1. Verify keystore file exists and path is correct
2. Check passwords in `keystore.properties`
3. Ensure key alias is correct
4. Try manual signing with jarsigner

---

**Problem**: `Unable to update app (INSTALL_FAILED_UPDATE_INCOMPATIBLE)`

**Cause**: Different signing certificate

**Solution**:
```bash
# Uninstall old version first
adb uninstall com.lpu5.tactical

# Then install new version
adb install app-release.apk
```

---

### Installation Issues

**Problem**: `App not installed`

**Common Causes & Solutions**:

1. **Insufficient Storage**:
   - Free up space on device
   - Uninstall unused apps

2. **Package Name Conflict**:
   - Uninstall existing version
   - Check for test/debug versions

3. **Android Version Too Old**:
   - Update Android OS
   - Build APK with lower minSdk (if possible)

4. **Corrupted APK**:
   - Re-download APK
   - Verify SHA-256 checksum

---

**Problem**: `Unknown sources not working`

**Android 8.0+**:
- Settings → Apps → Special app access → Install unknown apps
- Enable for browser/file manager used for installation

**Android 7.x**:
- Settings → Security → Unknown sources → Enable

---

## Distribution Tracking

### Track Installations

For internal distribution, track:

- Device ID / User name
- Version installed
- Installation date
- Installation method

**Example Tracking Sheet**:

| Device | User | Version | Date | Method | Status |
|--------|------|---------|------|--------|--------|
| Device-001 | John | 1.0.0 | 2024-02-13 | ADB | Active |
| Device-002 | Jane | 1.0.0 | 2024-02-13 | Web | Active |

### Analytics (Optional)

Consider adding analytics:

- Firebase Analytics
- Google Analytics
- Custom analytics server

Track:
- Active installations
- Crash reports
- Feature usage
- Performance metrics

---

## Support Resources

### Documentation

- [Android README](README.md) - Full Android app documentation
- [Main README](../README.md) - Project overview
- [DEPLOYMENT.md](../DEPLOYMENT.md) - Complete deployment guide

### Community

- **Issues**: [GitHub Issues](https://github.com/fredy060795/lpu5-tactical/issues)
- **Discussions**: [GitHub Discussions](https://github.com/fredy060795/lpu5-tactical/discussions)
- **Email**: [Your support email]

### External Resources

- [Android Developer Guide](https://developer.android.com/studio/publish)
- [Google Play Console Help](https://support.google.com/googleplay/android-developer)
- [Meshtastic Documentation](https://meshtastic.org/docs/)

---

## Quick Reference Commands

```bash
# Build debug APK
./gradlew assembleDebug

# Build release APK
./gradlew assembleRelease

# Build App Bundle (for Play Store)
./gradlew bundleRelease

# Install via ADB
adb install app-release.apk

# Uninstall
adb uninstall com.lpu5.tactical

# View device logs
adb logcat | grep LPU5

# Generate checksum
sha256sum app-release.apk

# Create QR code for download
qrencode -o qr.png "http://server.com/app.apk"
```

---

**Last Updated**: 2024-02-13  
**Version**: 1.0  
**Maintained By**: [Your Team/Organization]
