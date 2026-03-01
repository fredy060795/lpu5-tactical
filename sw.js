const CACHE_NAME = 'aegis-v2-meshtastic';
const ASSETS = [
    '/',
    '/landing.html',
    '/mobile.html',
    '/tactical_map.html',
    '/index.html',
    '/overview.html',
    '/meshtastic-web-client.js',
    '/cot-client.js',
    '/message-queue-manager.js',
    '/manifest.json',
    '/logo.png',
    '/assets/api-client.js',
    '/assets/ws-client.js',
    'https://unpkg.com/dexie/dist/dexie.js',
    'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css',
    'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js',
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css'
];

// Install Event
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            console.log('SW: Caching static assets');
            return cache.addAll(ASSETS);
        })
    );
    self.skipWaiting();
});

// Activate Event
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys => {
            return Promise.all(
                keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
            );
        })
    );
    self.clients.claim();
});

// Fetch Event
self.addEventListener('fetch', event => {
    // Skip cross-origin requests (except CDNs in ASSETS)
    if (!event.request.url.startsWith(self.location.origin) && !ASSETS.some(a => event.request.url.startsWith(a))) {
        return;
    }

    // Skip API calls - they should be handled by api-client.js logic
    if (event.request.url.includes('/api/')) {
        return;
    }

    event.respondWith(
        caches.match(event.request).then(cachedResponse => {
            // Return cached response if found, otherwise fetch from network
            const networkFetch = fetch(event.request).then(response => {
                // Update cache with new version if it's a static asset
                if (response && response.status === 200 && response.type === 'basic') {
                    const responseClone = response.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, responseClone));
                }
                return response;
            });

            return cachedResponse || networkFetch;
        }).catch(() => {
            // Offline fallback for HTML pages
            if (event.request.mode === 'navigate') {
                return caches.match('/mobile.html');
            }
        })
    );
});
