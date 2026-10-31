const STATIC_CACHE = "ponkun-static-v1";
const STATIC_ASSETS = [
  "/manifest.webmanifest",
  "/manifest.json"
];

function isPrivateOrDynamicPath(pathname) {
  return [
    "/auth",
    "/chat",
    "/history",
    "/sessions",
    "/voice",
    "/vision",
    "/api",
    "/health",
    "/readiness"
  ].some((prefix) => pathname === prefix || pathname.startsWith(prefix + "/"));
}

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(STATIC_CACHE).then((cache) => cache.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== STATIC_CACHE).map((key) => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || isPrivateOrDynamicPath(url.pathname)) {
    return;
  }

  // Navigation and non-static pages are network-only: authenticated HTML and history must not be cached.
  if (request.mode === "navigate" || !STATIC_ASSETS.includes(url.pathname)) {
    event.respondWith(fetch(request));
    return;
  }

  event.respondWith(
    fetch(request).then((response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(STATIC_CACHE).then((cache) => cache.put(request, copy));
      }
      return response;
    }).catch(() => caches.match(request))
  );
});
