const CACHE_NAME = "pickbook-cache-v1";

// Assets to pre-cache on install
const PRECACHE_URLS = [
  "/",
  "/manifest.json",
];

// Bug fix 1: install handler opened the cache but never added anything to it.
self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(PRECACHE_URLS))
  );
  // Activate the new SW immediately instead of waiting for old tabs to close.
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  // Remove stale caches from previous SW versions.
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// Bug fix 2: fetch handler tried the network first but never stored successful
// responses in the cache, so offline fallback only worked for pre-cached URLs.
// Now uses a stale-while-revalidate pattern: serve from cache instantly, then
// update the cache entry in the background.
self.addEventListener("fetch", event => {
  // Only cache GET requests; skip cross-origin requests.
  if (event.request.method !== "GET") return;

  event.respondWith(
    caches.open(CACHE_NAME).then(async cache => {
      const cached = await cache.match(event.request);

      const networkFetch = fetch(event.request)
        .then(response => {
          // Only cache valid, same-origin responses.
          if (response && response.status === 200 && response.type === "basic") {
            cache.put(event.request, response.clone());
          }
          return response;
        })
        .catch(() => cached); // Fall back to cache if network fails.

      // Return cached version immediately (if available), then update behind the scenes.
      return cached || networkFetch;
    })
  );
});
