/* Service worker.
 *
 * Caches the app shell so the dashboard opens instantly from the home screen,
 * and keeps the last successful GET of each API endpoint so a launch with the
 * server unreachable shows something rather than an error page.
 *
 * The honesty rule that shapes this file: a cached biometric is a *stale*
 * biometric. Anything served from cache gets an `X-Served-From-Cache` header and
 * the time it was stored, and the UI turns that into a visible "showing data
 * from HH:MM — offline" banner. Silently rendering yesterday's recovery as
 * today's would be the worst bug this app could have.
 *
 * Writes are never queued. An alarm or journal entry that did not reach the
 * server has not happened, and pretending otherwise would be a lie you would
 * discover at 6am.
 *
 * NOTE: service workers require a secure context. Over plain http:// on a LAN
 * address this file never registers — see README, "Installing on your phone".
 */

const VERSION = 'strap-v1';
const SHELL_CACHE = `${VERSION}-shell`;
const DATA_CACHE = `${VERSION}-data`;

const SHELL = [
  '/',
  '/static/styles.css',
  '/static/app.js',
  '/static/chart.js',
  '/static/journal.js',
  '/static/workouts.js',
  '/static/alarms.js',
  '/static/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

/** GETs worth keeping a copy of. Everything else is network-only. */
const CACHEABLE_API = [
  '/api/today',
  '/api/metrics/meta',
  '/api/trends',
  '/api/journal',
  '/api/workouts',
  '/api/alarms',
  '/api/heart-rate',
  '/api/strap/state',
  '/api/diagnostics',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      // addAll is all-or-nothing; one 404 would leave the app uncached.
      .then((cache) => Promise.allSettled(SHELL.map((url) => cache.add(url))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k)),
      ))
      .then(() => self.clients.claim()),
  );
});

function isCacheableApi(url) {
  return CACHEABLE_API.some((path) => url.pathname === path
    || url.pathname.startsWith(`${path}/`));
}

/** Re-wrap a cached response so the client can tell it is stale. */
async function markStale(response) {
  const body = await response.blob();
  const headers = new Headers(response.headers);
  headers.set('X-Served-From-Cache', '1');
  if (!headers.get('X-Cached-At')) headers.set('X-Cached-At', '0');
  return new Response(body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

async function networkFirst(request) {
  const cache = await caches.open(DATA_CACHE);
  try {
    const fresh = await fetch(request);
    if (fresh.ok) {
      const headers = new Headers(fresh.headers);
      headers.set('X-Cached-At', String(Date.now()));
      const copy = new Response(await fresh.clone().blob(), {
        status: fresh.status, statusText: fresh.statusText, headers,
      });
      cache.put(request, copy);
    }
    return fresh;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return markStale(cached);
    // Nothing cached either: a JSON error the app can render, not a browser page.
    return new Response(
      JSON.stringify({
        error: 'offline',
        detail: 'The dashboard is not reachable, and there is no cached copy of '
              + 'this yet. Check the machine running it is awake and on the same network.',
      }),
      { status: 503, headers: { 'Content-Type': 'application/json' } },
    );
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const fresh = await fetch(request);
  if (fresh.ok) (await caches.open(SHELL_CACHE)).put(request, fresh.clone());
  return fresh;
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;                 // never queue writes

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith('/api/')) {
    if (isCacheableApi(url)) event.respondWith(networkFirst(request));
    // Exports and anything else stay network-only: a stale export would be worse
    // than no export.
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/').then((r) => r || Response.error())),
    );
    return;
  }

  event.respondWith(cacheFirst(request));
});
