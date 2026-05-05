/* Task Tracker service worker — minimal app-shell + network-first for /api/. */
const CACHE = "tt-shell-v1";
const SHELL = ["/", "/static/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // Never cache API or auth routes — always go network.
  if (
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/login") ||
    url.pathname.startsWith("/signup") ||
    url.pathname.startsWith("/logout") ||
    url.pathname.startsWith("/forgot-password") ||
    url.pathname.startsWith("/reset-password") ||
    url.pathname.startsWith("/join")
  ) {
    return;
  }

  // App shell: network first, fall back to cache.
  event.respondWith(
    fetch(req)
      .then((res) => {
        const clone = res.clone();
        caches.open(CACHE).then((c) => c.put(req, clone)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(req).then((r) => r || caches.match("/")))
  );
});
