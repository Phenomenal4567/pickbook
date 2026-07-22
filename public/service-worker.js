const CACHE_NAME = "pickbook-cache-v2";
const DEFAULT_NOTIFICATION_URL = "/";
const DEFAULT_ICON = "/static/favicon.ico";
const DEFAULT_BADGE = "/static/favicon.ico";

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

  const url = new URL(event.request.url);
  const isSameOrigin = url.origin === self.location.origin;
  const isNavigation = event.request.mode === "navigate";
  const isApiRequest = isSameOrigin && (
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/author/") ||
    url.pathname.startsWith("/admin/")
  );

  if (isNavigation || isApiRequest) {
    event.respondWith(
      fetch(event.request).catch(() => caches.match(event.request))
    );
    return;
  }

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

function safeNotificationPayload(event) {
  if (!event.data) {
    return {};
  }

  try {
    return event.data.json();
  } catch (_error) {
    return {
      title: "PickBook",
      body: event.data.text(),
    };
  }
}

function normalizeTargetUrl(value) {
  try {
    const url = new URL(value || DEFAULT_NOTIFICATION_URL, self.location.origin);
    if (url.origin !== self.location.origin) {
      return DEFAULT_NOTIFICATION_URL;
    }
    return `${url.pathname}${url.search}${url.hash}`;
  } catch (_error) {
    return DEFAULT_NOTIFICATION_URL;
  }
}

self.addEventListener("push", event => {
  const payload = safeNotificationPayload(event);
  const data = payload.data && typeof payload.data === "object" ? payload.data : {};
  const targetUrl = normalizeTargetUrl(data.url || payload.url);

  const title = String(payload.title || "PickBook").slice(0, 120);
  const options = {
    body: String(payload.body || "You have a new PickBook update.").slice(0, 240),
    icon: payload.icon || DEFAULT_ICON,
    badge: payload.badge || DEFAULT_BADGE,
    data: {
      ...data,
      url: targetUrl,
    },
    tag: data.tag || targetUrl,
    renotify: Boolean(data.renotify),
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();

  const targetUrl = normalizeTargetUrl(event.notification.data && event.notification.data.url);
  const absoluteTarget = new URL(targetUrl, self.location.origin).href;

  event.waitUntil((async () => {
    const windowClients = await self.clients.matchAll({
      type: "window",
      includeUncontrolled: true,
    });

    for (const client of windowClients) {
      const clientUrl = new URL(client.url);
      if (clientUrl.origin === self.location.origin) {
        await client.navigate(absoluteTarget);
        return client.focus();
      }
    }

    return self.clients.openWindow(absoluteTarget);
  })());
});
