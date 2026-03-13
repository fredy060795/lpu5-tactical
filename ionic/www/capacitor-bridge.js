/**
 * capacitor-bridge.js
 *
 * Bridges Capacitor native plugins (Geolocation, BluetoothLe, Toast) to the
 * existing LPU5 Tactical web code so the same HTML/JS works on Android, iOS
 * and in a desktop browser without any changes to the app logic.
 *
 * Exposed globals after this script runs:
 *   window.isCapacitorNative  – true when running inside a Capacitor shell
 *   window.isAndroidNative    – kept for backwards compat with existing code
 *   window.isIOSNative        – true when running inside Capacitor on iOS
 *   window.hasNativeMeshtastic– true on both Android and iOS Capacitor (BLE)
 *   window.nativeGetPosition()      – returns current GPS position (JSON string)
 *   window.nativeConnectMeshtastic()– open BLE device picker and connect
 *   window.nativeSendMessage(msg, isCOT) – send a text/COT message via BLE
 *   window.nativeGetMeshtasticNodes() – return connected BLE node list (JSON)
 *   window.nativeDisconnectMeshtastic() – disconnect current BLE device
 *   window.Android.showToast(msg)   – show a native toast notification
 *   window.onAndroidEvent(event, data) – override this to receive native events
 */

(function () {
  'use strict';

  // ── Detect Capacitor runtime ──────────────────────────────────────────────
  const isCapacitor = !!(window.Capacitor && window.Capacitor.isNativePlatform && window.Capacitor.isNativePlatform());

  // Detect specific platform (iOS vs Android)
  var capPlatform = '';
  if (isCapacitor && window.Capacitor.getPlatform) {
    capPlatform = window.Capacitor.getPlatform(); // 'ios', 'android', or 'web'
  }

  window.isCapacitorNative = isCapacitor;
  // Keep backwards-compatible flag that existing code checks
  window.isAndroidNative = isCapacitor && capPlatform !== 'ios';
  // iOS native flag – Meshtastic BLE is now fully supported on iOS via Capacitor
  window.isIOSNative = isCapacitor && capPlatform === 'ios';
  // Both Android and iOS support direct BLE Meshtastic via Capacitor
  window.hasNativeMeshtastic = isCapacitor;

  if (!isCapacitor) {
    console.log('[CapacitorBridge] Running in browser – native features disabled.');
    // Provide no-op stubs so callers don't get undefined errors in browser mode
    window.nativeGetPosition        = function () { return '{}'; };
    window.nativeConnectMeshtastic  = function () { console.log('[CapacitorBridge] BLE not available in browser.'); };
    window.nativeDisconnectMeshtastic = function () { console.log('[CapacitorBridge] BLE not available in browser.'); };
    window.nativeSendMessage        = function () { console.log('[CapacitorBridge] BLE not available in browser.'); };
    window.nativeGetMeshtasticNodes = function () { return '[]'; };
    window.Android = window.Android || {};
    window.Android.showToast        = function (msg) { console.log('[CapacitorBridge] Toast (browser):', msg); };
    return;
  }

  console.log('[CapacitorBridge] Capacitor native platform detected (' + (capPlatform || 'unknown') + '), initialising bridge…');

  // ── Helper: fire web-facing event (same contract as the old Android bridge) ──
  function fireEvent(event, dataObj) {
    if (typeof window.onAndroidEvent === 'function') {
      window.onAndroidEvent(event, JSON.stringify(dataObj));
    }
  }

  // ══════════════════════════════════════════════════════════════════════════
  // 1. GEOLOCATION  (@capacitor/geolocation)
  // ══════════════════════════════════════════════════════════════════════════
  const { Geolocation } = window.Capacitor.Plugins;
  let _currentPosition = null;
  let _watchId = null;

  async function startLocationTracking() {
    try {
      await Geolocation.requestPermissions();
      _watchId = await Geolocation.watchPosition(
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 },
        function (position, err) {
          if (err) {
            console.warn('[CapacitorBridge] Geolocation error:', err);
            return;
          }
          _currentPosition = {
            latitude:  position.coords.latitude,
            longitude: position.coords.longitude,
            altitude:  position.coords.altitude  || 0,
            accuracy:  position.coords.accuracy  || 0,
            timestamp: position.timestamp
          };
          fireEvent('locationUpdate', _currentPosition);
        }
      );
      console.log('[CapacitorBridge] Location tracking started, watchId:', _watchId);
    } catch (e) {
      console.warn('[CapacitorBridge] Could not start location tracking:', e);
    }
  }

  /**
   * Returns a JSON string with the last known position, or "{}" if unavailable.
   * Replaces Android.getCurrentPosition() / window.nativeGetPosition().
   */
  window.nativeGetPosition = function () {
    return _currentPosition ? JSON.stringify(_currentPosition) : '{}';
  };

  // ══════════════════════════════════════════════════════════════════════════
  // 2. BLUETOOTH LE  (@capacitor-community/bluetooth-le)
  //    Works on BOTH Android and iOS via Capacitor native BLE plugin.
  //    iOS now fully supports Meshtastic BLE connections.
  // ══════════════════════════════════════════════════════════════════════════
  const { BluetoothLe } = window.Capacitor.Plugins;
  let _bleDevice = null;
  let _bleConnected = false;

  // Meshtastic BLE service / characteristic UUIDs
  const MESH_SERVICE_UUID    = '6ba1b218-15a8-461f-9fa8-5dcae273eafd';
  const MESH_TORADIO_UUID    = 'f75c76d2-129e-4dad-a1dd-7866124401e7';
  const MESH_FROMRADIO_UUID  = '8ba2bcc2-ee02-4a55-a531-c525c5e454d5';
  const MESH_FROMNUM_UUID    = 'ed9da18c-a800-4f66-a670-aa7547e34453';

  /**
   * Show the BLE device picker and connect to a Meshtastic device.
   * Works on both Android and iOS via @capacitor-community/bluetooth-le.
   * Replaces Android.connectMeshtastic() / window.nativeConnectMeshtastic().
   */
  window.nativeConnectMeshtastic = async function () {
    try {
      await BluetoothLe.initialize();

      // On iOS, request BLE permissions explicitly (CoreBluetooth)
      if (capPlatform === 'ios') {
        try {
          await BluetoothLe.requestLEScan({ allowDuplicates: false });
          await BluetoothLe.stopLEScan();
        } catch (scanErr) {
          console.log('[CapacitorBridge] iOS BLE permission pre-check:', scanErr.message);
        }
      }

      var scanOpts = { services: [MESH_SERVICE_UUID] };
      // On iOS, optionally allow all devices if service filter fails
      if (capPlatform === 'ios') {
        scanOpts.optionalServices = [MESH_SERVICE_UUID];
      }

      const result = await BluetoothLe.requestDevice(scanOpts);
      _bleDevice = result;

      await BluetoothLe.connect({ deviceId: _bleDevice.deviceId });
      _bleConnected = true;

      // Subscribe to incoming radio packets
      await BluetoothLe.startNotifications({
        deviceId: _bleDevice.deviceId,
        service:  MESH_SERVICE_UUID,
        characteristic: MESH_FROMNUM_UUID
      });
      BluetoothLe.addListener('onNotification', function (data) {
        fireEvent('meshtasticMessage', { raw: data.value });
      });

      var deviceLabel = _bleDevice.name || _bleDevice.deviceId;
      console.log('[CapacitorBridge] BLE connected to', deviceLabel, '(platform:', capPlatform, ')');
      fireEvent('meshtasticServiceConnected', { status: 'connected', device: deviceLabel, platform: capPlatform });
      window.Android && window.Android.showToast('Meshtastic verbunden: ' + deviceLabel);
    } catch (e) {
      console.warn('[CapacitorBridge] BLE connect error:', e);
      fireEvent('meshtasticServiceDisconnected', { status: 'error', message: e.message });
      window.Android && window.Android.showToast('BLE Fehler: ' + e.message);
    }
  };

  /**
   * Disconnect from the currently connected Meshtastic BLE device.
   */
  window.nativeDisconnectMeshtastic = async function () {
    if (!_bleDevice) return;
    try {
      await BluetoothLe.stopNotifications({
        deviceId: _bleDevice.deviceId,
        service:  MESH_SERVICE_UUID,
        characteristic: MESH_FROMNUM_UUID
      });
    } catch (e) { /* ignore */ }
    try {
      await BluetoothLe.disconnect({ deviceId: _bleDevice.deviceId });
    } catch (e) { /* ignore */ }
    _bleConnected = false;
    var deviceLabel = _bleDevice ? (_bleDevice.name || _bleDevice.deviceId) : '';
    _bleDevice = null;
    console.log('[CapacitorBridge] BLE disconnected', deviceLabel);
    fireEvent('meshtasticServiceDisconnected', { status: 'disconnected', device: deviceLabel });
    window.Android && window.Android.showToast('Meshtastic getrennt');
  };

  /**
   * Send a text or COT message to the connected Meshtastic device.
   * Replaces Android.sendMeshtasticMessage() / window.nativeSendMessage().
   */
  window.nativeSendMessage = async function (message, isCOT) {
    if (!_bleConnected || !_bleDevice) {
      window.Android && window.Android.showToast('Kein Meshtastic Gerät verbunden');
      return;
    }
    try {
      // Encode UTF-8 message to base64 for Capacitor BLE write
      const encoded = btoa(unescape(encodeURIComponent(message)));
      await BluetoothLe.write({
        deviceId:       _bleDevice.deviceId,
        service:        MESH_SERVICE_UUID,
        characteristic: MESH_TORADIO_UUID,
        value:          encoded
      });
      fireEvent('messageSent', { message: message, isCOT: !!isCOT });
    } catch (e) {
      console.warn('[CapacitorBridge] BLE send error:', e);
      window.Android && window.Android.showToast('Senden fehlgeschlagen: ' + e.message);
    }
  };

  /**
   * Returns the currently connected Meshtastic node(s) as a JSON array.
   * Replaces Android.getMeshtasticNodes() / window.nativeGetMeshtasticNodes().
   */
  window.nativeGetMeshtasticNodes = function () {
    if (!_bleDevice) return '[]';
    return JSON.stringify([{ id: _bleDevice.deviceId, name: _bleDevice.name || 'Unknown', connected: _bleConnected }]);
  };

  // ══════════════════════════════════════════════════════════════════════════
  // 3. TOAST  (@capacitor/toast)
  // ══════════════════════════════════════════════════════════════════════════
  const { Toast } = window.Capacitor.Plugins;

  // Provide window.Android.showToast() – existing code calls this API
  window.Android = window.Android || {};
  window.Android.showToast = async function (message) {
    try {
      await Toast.show({ text: String(message), duration: 'short' });
    } catch (e) {
      console.warn('[CapacitorBridge] Toast error:', e);
    }
  };

  // ── Auto-start location tracking ─────────────────────────────────────────
  startLocationTracking();

  console.log('[CapacitorBridge] Bridge initialised (Geolocation + BLE + Toast) on ' + (capPlatform || 'unknown') + '.');
  if (capPlatform === 'ios') {
    console.log('[CapacitorBridge] iOS detected – Meshtastic BLE is fully supported via Capacitor.');
  }
})();
