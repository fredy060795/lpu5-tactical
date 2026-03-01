package com.lpu5.tactical

import android.Manifest
import android.annotation.SuppressLint
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Bundle
import android.os.IBinder
import android.webkit.*
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.geeksville.mesh.IMeshService
import com.geeksville.mesh.MeshProtos
import com.geeksville.mesh.service.MeshService
import com.google.android.gms.location.*
import com.google.gson.Gson
import kotlinx.coroutines.*

/**
 * MainActivity for AEGIS Tactical Android Application
 * 
 * This activity provides:
 * - WebView hosting overview.html
 * - Native Meshtastic BLE/Serial integration
 * - GPS position tracking
 * - JavaScript bridge for WebView ↔ Native communication
 * - COT message exchange
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var meshService: IMeshService? = null
    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private var currentLocation: Location? = null
    private val gson = Gson()
    
    companion object {
        private const val PERMISSION_REQUEST_CODE = 100
        private val REQUIRED_PERMISSIONS = arrayOf(
            Manifest.permission.BLUETOOTH_CONNECT,
            Manifest.permission.BLUETOOTH_SCAN,
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION,
            Manifest.permission.CAMERA
        )
    }

    private val serviceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
            meshService = IMeshService.Stub.asInterface(service)
            runOnUiThread {
                Toast.makeText(this@MainActivity, "Meshtastic Service Connected", Toast.LENGTH_SHORT).show()
                notifyWebView("meshtasticServiceConnected", "{\"status\": \"connected\"}")
            }
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            meshService = null
            runOnUiThread {
                Toast.makeText(this@MainActivity, "Meshtastic Service Disconnected", Toast.LENGTH_SHORT).show()
                notifyWebView("meshtasticServiceDisconnected", "{\"status\": \"disconnected\"}")
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)
        
        initializeWebView()
        checkPermissions()
        bindMeshtasticService()
        startLocationTracking()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun initializeWebView() {
        webView = findViewById(R.id.webView)
        
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            allowFileAccess = true
            allowContentAccess = true
            setSupportZoom(true)
            builtInZoomControls = false
            loadWithOverviewMode = true
            useWideViewPort = true
            cacheMode = WebSettings.LOAD_DEFAULT
        }

        // Add JavaScript interface for native communication
        webView.addJavascriptInterface(WebAppInterface(this), "Android")
        
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                return false
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                // Inject Android-specific JavaScript
                injectAndroidBridge()
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onConsoleMessage(message: ConsoleMessage?): Boolean {
                message?.let {
                    android.util.Log.d("WebView", "${it.message()} -- From line ${it.lineNumber()} of ${it.sourceId()}")
                }
                return true
            }

            override fun onGeolocationPermissionsShowPrompt(
                origin: String?,
                callback: GeolocationPermissions.Callback?
            ) {
                callback?.invoke(origin, true, false)
            }

            override fun onPermissionRequest(request: PermissionRequest?) {
                request?.let {
                    // Grant camera and microphone access to the WebView
                    val granted = it.resources.filter { resource ->
                        resource == PermissionRequest.RESOURCE_VIDEO_CAPTURE ||
                        resource == PermissionRequest.RESOURCE_AUDIO_CAPTURE
                    }.toTypedArray()
                    if (granted.isNotEmpty()) {
                        it.grant(granted)
                    } else {
                        it.deny()
                    }
                }
            }
        }

        // Load the overview.html from assets
        webView.loadUrl("file:///android_asset/www/overview.html")
    }

    private fun injectAndroidBridge() {
        val script = """
            window.isAndroidNative = true;
            window.hasNativeMeshtastic = true;
            
            // Override Meshtastic functions to use native bridge
            window.nativeConnectMeshtastic = function() {
                Android.connectMeshtastic();
            };
            
            window.nativeSendMessage = function(message, isCOT) {
                Android.sendMeshtasticMessage(message, isCOT);
            };
            
            window.nativeGetPosition = function() {
                return Android.getCurrentPosition();
            };
            
            window.nativeGetMeshtasticNodes = function() {
                return Android.getMeshtasticNodes();
            };
            
            console.log('Android native bridge initialized');
        """.trimIndent()
        
        webView.evaluateJavascript(script, null)
    }

    private fun notifyWebView(event: String, data: String) {
        val script = """
            if (window.onAndroidEvent) {
                window.onAndroidEvent('$event', $data);
            }
        """.trimIndent()
        
        runOnUiThread {
            webView.evaluateJavascript(script, null)
        }
    }

    private fun checkPermissions() {
        val permissionsToRequest = REQUIRED_PERMISSIONS.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }

        if (permissionsToRequest.isNotEmpty()) {
            ActivityCompat.requestPermissions(
                this,
                permissionsToRequest.toTypedArray(),
                PERMISSION_REQUEST_CODE
            )
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        
        if (requestCode == PERMISSION_REQUEST_CODE) {
            val allGranted = grantResults.all { it == PackageManager.PERMISSION_GRANTED }
            if (allGranted) {
                Toast.makeText(this, "Permissions granted", Toast.LENGTH_SHORT).show()
                bindMeshtasticService()
                startLocationTracking()
            } else {
                Toast.makeText(this, "Permissions required for full functionality", Toast.LENGTH_LONG).show()
            }
        }
    }

    private fun bindMeshtasticService() {
        try {
            val intent = Intent(this, MeshService::class.java)
            bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)
        } catch (e: Exception) {
            Toast.makeText(this, "Failed to bind Meshtastic service: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    @SuppressLint("MissingPermission")
    private fun startLocationTracking() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) 
            == PackageManager.PERMISSION_GRANTED) {
            
            val locationRequest = LocationRequest.Builder(
                Priority.PRIORITY_HIGH_ACCURACY, 
                10000 // 10 seconds
            ).apply {
                setMinUpdateIntervalMillis(5000) // 5 seconds
                setMaxUpdateDelayMillis(15000) // 15 seconds
            }.build()

            fusedLocationClient.requestLocationUpdates(
                locationRequest,
                object : LocationCallback() {
                    override fun onLocationResult(locationResult: LocationResult) {
                        locationResult.lastLocation?.let { location ->
                            currentLocation = location
                            notifyLocationUpdate(location)
                        }
                    }
                },
                mainLooper
            )
        }
    }

    private fun notifyLocationUpdate(location: Location) {
        val locationData = """
            {
                "latitude": ${location.latitude},
                "longitude": ${location.longitude},
                "altitude": ${location.altitude},
                "accuracy": ${location.accuracy},
                "timestamp": ${location.time}
            }
        """.trimIndent()
        
        notifyWebView("locationUpdate", locationData)
    }

    inner class WebAppInterface(private val context: Context) {
        
        @JavascriptInterface
        fun connectMeshtastic() {
            // Meshtastic service is already bound, just notify
            runOnUiThread {
                Toast.makeText(context, "Connecting to Meshtastic...", Toast.LENGTH_SHORT).show()
            }
        }

        @JavascriptInterface
        fun sendMeshtasticMessage(message: String, isCOT: Boolean) {
            meshService?.let { service ->
                try {
                    // Create text message packet
                    val textProto = MeshProtos.Data.newBuilder()
                        .setPortnum(MeshProtos.PortNum.TEXT_MESSAGE_APP)
                        .setPayload(com.google.protobuf.ByteString.copyFromUtf8(message))
                        .build()

                    // Send to all nodes (broadcast)
                    service.send(textProto)
                    
                    runOnUiThread {
                        Toast.makeText(context, "Message sent via Meshtastic", Toast.LENGTH_SHORT).show()
                        notifyWebView("messageSent", "{\"message\": \"${message.replace("\"", "\\\"")}\"}")
                    }
                } catch (e: Exception) {
                    runOnUiThread {
                        Toast.makeText(context, "Failed to send: ${e.message}", Toast.LENGTH_LONG).show()
                    }
                }
            } ?: run {
                runOnUiThread {
                    Toast.makeText(context, "Meshtastic service not connected", Toast.LENGTH_SHORT).show()
                }
            }
        }

        @JavascriptInterface
        fun getCurrentPosition(): String {
            return currentLocation?.let {
                """
                {
                    "latitude": ${it.latitude},
                    "longitude": ${it.longitude},
                    "altitude": ${it.altitude},
                    "accuracy": ${it.accuracy}
                }
                """.trimIndent()
            } ?: "{}"
        }

        @JavascriptInterface
        fun getMeshtasticNodes(): String {
            // TODO: Implement node retrieval from Meshtastic service
            return "[]"
        }

        @JavascriptInterface
        fun showToast(message: String) {
            runOnUiThread {
                Toast.makeText(context, message, Toast.LENGTH_SHORT).show()
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        try {
            unbindService(serviceConnection)
        } catch (e: Exception) {
            // Service may not be bound
        }
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }
}
