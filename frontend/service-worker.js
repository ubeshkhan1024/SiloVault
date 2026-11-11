/**
 * SiloVault Service Worker (PWA Foundation).
 *
 * This is a minimal service worker skeleton. It registers the app for offline
 * caching of the shell (HTML, CSS, JS) and handles basic lifecycle events.
 *
 * IMPORTANT: This does NOT implement background uploading or reliable offline
 * file synchronization. Those features are planned for future phases.
 */

/**
 * Increment this value whenever frontend shell assets change.
 * The new value drives a new CACHE_NAME, causing the activate handler to
 * delete the old cache and fetch all assets afresh on the next page load.
 *
 * Do NOT reset this to 1 after a bump — monotonic increment only.
 */
const CACHE_VERSION = '2';

const CACHE_NAME = `silovault-shell-v${CACHE_VERSION}`;

/**
 * Shell assets to cache on install.  Includes every statically-served
 * client-side file so that a single CACHE_VERSION bump invalidates them all
 * atomically — no partial staleness between JS modules.
 */
const SHELL_ASSETS = [
    "./index.html",
    "./css/style.css",
    "./js/app.js",
    "./js/api.js",
    "./js/dashboard.js",
    "./js/activity.js",
    "./js/files.js",
    "./js/uploads.js",
    "./js/settings.js",
    "./manifest.json",
];

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(SHELL_ASSETS);
        })
    );
    // New worker takes control immediately — no waiting for existing tabs
    // to close before it starts serving requests.
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) => {
            // Delete every cache whose name does not match the CURRENT version.
            // This is safe: only shell assets are stored in Cache API — user
            // data (database, uploaded files) lives on the filesystem and is
            // unaffected.
            return Promise.all(
                keys
                    .filter((key) => key !== CACHE_NAME)
                    .map((key) => caches.delete(key))
            );
        })
    );
    // Claim all controlled pages so the new cache is used immediately
    // without requiring the user to navigate away and back.
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
