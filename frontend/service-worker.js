/**
 * SiloVault Service Worker (PWA Foundation).
 *
 * This is a minimal service worker skeleton. It registers the app for offline
 * caching of the shell (HTML, CSS, JS) and handles basic lifecycle events.
 *
 * IMPORTANT: This does NOT implement background uploading or reliable offline
 * file synchronization. Those features are planned for future phases.
 */

const CACHE_NAME = "silovault-shell-v1";
const SHELL_ASSETS = [
    "./index.html",
    "./css/style.css",
    "./js/app.js",
    "./js/api.js",
    "./manifest.json",
];

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(SHELL_ASSETS);
        })
    );
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) => {
            return Promise.all(
                keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
            );
        })
    );
    self.clients.claim();
});

self.addEventListener("fetch", (event) => {
    const request = event.request;

    // Only handle same-origin GET requests.
    if (request.method !== "GET" || !request.url.startsWith(self.location.origin)) {
        return;
    }

    event.respondWith(
        caches.match(request).then((cachedResponse) => {
            if (cachedResponse) {
                return cachedResponse;
            }

            return fetch(request)
                .then((response) => {
                    // Do not cache non-OK responses.
                    if (!response || response.status !== 200) {
                        return response;
                    }

                    // Clone and cache the response for future offline use.
                    const responseToCache = response.clone();
                    caches.open(CACHE_NAME).then((cache) => {
                        cache.put(request, responseToCache);
                    });

                    return response;
                })
                .catch(() => {
                    // Offline fallback: return cached shell or a basic offline page.
                    return caches.match("./index.html");
                });
        })
    );
});