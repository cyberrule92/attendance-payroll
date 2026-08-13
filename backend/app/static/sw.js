/* Service worker for the kiosk.
 *
 * Scope is deliberately narrow: cache the app shell so the kiosk still opens
 * when the office laptop is off, and stay out of the way of everything else.
 *
 * The punch queue is NOT handled here. It lives in IndexedDB driven by the
 * page, because Background Sync is still missing on iOS and a queue that
 * silently does nothing on half the devices is worse than no queue at all.
 */

const CACHE = "attendance-shell-v1";
const SHELL = ["/kiosk", "/static/app.css", "/static/icon.svg", "/manifest.webmanifest"];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // API responses must never be served stale -- a cached roster would show
  // yesterday's in/out times as if they were today's.
  if (url.pathname.startsWith("/api/")) return;

  // Network first, falling back to the cached shell when the server is down.
  event.respondWith(
    fetch(request)
      .then(response => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then(cache => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then(hit => hit || caches.match("/kiosk")))
  );
});
