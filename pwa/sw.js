const CACHE_NAME = 'aegis-pwa-v3-ios';
const ASSETS = [
    '/pwa/',
    '/pwa/overview.html',
    '/pwa/manifest.json',
    '/pwa/logo.png',
    '/pwa/meshtastic-web-client.js',
    '/pwa/cot-client.js',
    '/pwa/message-queue-manager.js',
    '/pwa/permissions.js',
    '/pwa/admin_users.js',
    '/pwa/load-global-nav.js',
    'https://unpkg.com/dexie/dist/dexie.js',
    'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css',
    'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js',
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css'
];

// Install Event - Cache PWA assets
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            console.log('[PWA SW] Caching PWA assets');
            return cache.addAll(ASSETS);
        })
    );
    self.skipWaiting();
});

// Activate Event - Clean up old caches
self.addEventListener('activate', event => {
    console.log('[PWA SW] Activating service worker');
    event.waitUntil(
        caches.keys().then(keys => {
            return Promise.all(
                keys.filter(key => key !== CACHE_NAME).map(key => {
                    console.log('[PWA SW] Deleting old cache:', key);
                    return caches.delete(key);
                })
            );
        })
    );
    self.clients.claim();
});

// Fetch Event - Network-first for API, cache-first for assets
self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);
    
    // Skip cross-origin requests (except CDNs in ASSETS)
    if (url.origin !== self.location.origin && !ASSETS.some(a => event.request.url.startsWith(a))) {
        return;
    }

    // Skip API calls - always fetch fresh data
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
            }).catch(() => {
                console.log('[PWA SW] Network fetch failed, returning cached version');
                if (cachedResponse) {
                    return cachedResponse;
                }
                // No cached version available
                return new Response('Offline - content not available', {
                    status: 503,
                    statusText: 'Service Unavailable'
                });
            });

            return cachedResponse || networkFetch;
        }).catch(() => {
            // Offline fallback for HTML pages
            if (event.request.mode === 'navigate') {
                return caches.match('/pwa/overview.html');
            }
        })
    );
});
