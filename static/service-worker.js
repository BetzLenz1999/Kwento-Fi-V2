/**
 * GoodMarket service worker.
 *
 * Strategy:
 * - Network-first for navigations and API calls (so users always see the
 *   latest deployment and live data when online).
 * - Cache-first for /static/ assets (which are versioned via ?v=ASSET_VERSION
 *   in templates, so a new deployment busts the cache automatically).
 * - On every activation, all old caches that don't match the current version
 *   are deleted.
 *
 * Auto-update mechanism:
 *   `__BUILD_VERSION__` is replaced at request time by the Flask route that
 *   serves this file (see `_dynamic_service_worker` in main.py). Embedding the
 *   build version in the SW body guarantees the file content changes on every
 *   deployment, so the browser's byte-for-byte SW comparison detects an update
 *   and triggers install -> waiting -> activation after the old clients close
 *   or an explicit user-approved SKIP_WAITING -> SW_UPDATED.
 *   If the file is ever served raw without substitution, the literal string
 *   below is used as a safe fallback (caches still work, just without
 *   per-deploy invalidation).
 */

const BUILD_VERSION = '__BUILD_VERSION__';
const CACHE_VERSION = 'goodmarket-__BUILD_VERSION__';
const STATIC_CACHE = `${CACHE_VERSION}-static`;

self.addEventListener('install', (event) => {
  // Do not force immediate activation. The worker waits while old clients are
  // open; if they all close, the browser may activate it automatically. The
  // root gate's accepted-build marker still requires the next launch to get
  // the user's explicit "Update app" acknowledgment.
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'GET_BUILD_VERSION') {
    const replyPort = event.ports && event.ports[0];
    if (replyPort) replyPort.postMessage({ type: 'BUILD_VERSION', version: BUILD_VERSION });
    return;
  }
  if (event.data && event.data.type === 'SKIP_WAITING') {
    event.waitUntil(self.skipWaiting());
  }
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys
        .filter((key) => !key.startsWith(CACHE_VERSION))
        .map((key) => caches.delete(key))
    );
    await self.clients.claim();
    // Tell open pages we updated so they can show a refresh prompt if desired.
    const clients = await self.clients.matchAll({ includeUncontrolled: true });
    for (const client of clients) {
      try { client.postMessage({ type: 'SW_UPDATED', version: CACHE_VERSION }); } catch (_) {}
    }
  })());
});


self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) {}
  const title = String(data.title || 'GoodMarket message').slice(0, 120);
  const body = String(data.body || 'You have a new community message.').slice(0, 240);
  const target = String(data.url || '/messages');
  event.waitUntil(self.registration.showNotification(title, {
    body,
    icon: '/static/icons/goodmarket-only-512.png',
    badge: '/static/icons/goodmarket-only-192.png',
    tag: String(data.tag || 'community-message').slice(0, 120),
    renotify: true,
    data: { url: target.startsWith('/') ? target : '/messages' }
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = event.notification.data && event.notification.data.url || '/messages';
  event.waitUntil((async () => {
    const absolute = new URL(target, self.location.origin).href;
    const openClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of openClients) {
      if ('focus' in client) {
        await client.focus();
        if ('navigate' in client) await client.navigate(absolute);
        return;
      }
    }
    if (self.clients.openWindow) await self.clients.openWindow(absolute);
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;

  // Only handle GETs on our origin.
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Cache-first for /static/ assets.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith((async () => {
      const cache = await caches.open(STATIC_CACHE);
      const cached = await cache.match(req);
      if (cached) return cached;
      try {
        const fresh = await fetch(req);
        if (fresh && fresh.ok && fresh.type === 'basic') {
          cache.put(req, fresh.clone()).catch(() => {});
        }
        return fresh;
      } catch (_) {
        return cached || Response.error();
      }
    })());
    return;
  }

  // Network-first for everything else (HTML pages, /api/*, etc.).
  event.respondWith((async () => {
    try {
      return await fetch(req);
    } catch (_) {
      const cached = await caches.match(req);
      return cached || Response.error();
    }
  })());
});
