/* ─────────────────────────────────────────────────────────────────────────────
 * Pushkaralu Pilgrim Portal — Service Worker
 *
 * Strategies:
 *   • App shell  (HTML, manifest, icons, fonts, Leaflet CDN)  → cache-first
 *   • OSM tiles  (a.b.c.tile.openstreetmap.org)              → stale-while-revalidate, capped
 *   • API GETs   (pushkara.onrender.com/get_*, /contacts...) → network-first, cache fallback
 *   • Anything POST / non-GET                                → never cached, pass-through
 *
 * The SW registers ONLY from user.html so admin/volunteer dashboards remain
 * unaffected on first visit. Once installed, scope is "/", but non-handled
 * requests (admin.html, /ws/*, etc.) fall through transparently.
 *
 * Bump SW_VERSION whenever the shell changes — the activate handler purges
 * stale caches automatically.
 * ────────────────────────────────────────────────────────────────────────── */

const SW_VERSION    = 'v1.1.0';
const SHELL_CACHE   = `pushkara-shell-${SW_VERSION}`;
const TILE_CACHE    = `pushkara-tiles-${SW_VERSION}`;
const API_CACHE     = `pushkara-api-${SW_VERSION}`;
const TILE_MAX_ITEMS = 500;     // ~15 MB at ~30 KB / tile
const API_MAX_AGE_MS = 5 * 60 * 1000;  // serve cached API response when offline / 5-min stale

const SHELL_URLS = [
  '/user',
  '/manifest.json',
  '/icon-192.svg',
  '/icon-512.svg',
  '/icon-maskable.svg',
  '/icon-192.png',
  '/icon-512.png',
  '/icon-maskable-512.png',
  // Leaflet from CDN — used by the ghat map
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
];

// ─── INSTALL ────────────────────────────────────────────────────────────────
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) =>
      // addAll is atomic; if any URL fails the install fails. Use individual
      // adds with catch so a single CDN hiccup doesn't break PWA install.
      Promise.all(
        SHELL_URLS.map((u) =>
          cache.add(new Request(u, { credentials: 'omit' })).catch((err) => {
            console.warn('[SW] shell precache miss:', u, err);
          })
        )
      )
    ).then(() => self.skipWaiting())
  );
});

// ─── ACTIVATE — purge old caches ────────────────────────────────────────────
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k.startsWith('pushkara-') &&
                        k !== SHELL_CACHE && k !== TILE_CACHE && k !== API_CACHE)
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// ─── HELPERS ────────────────────────────────────────────────────────────────
function isTileRequest(url) {
  return /\/\/[a-c]?\.?tile\.openstreetmap\.org\//i.test(url) ||
         /\.tile\.openstreetmap\.org\//i.test(url);
}

function isShellRequest(url) {
  if (SHELL_URLS.some((s) => url === s || url.endsWith(s))) return true;
  // Fonts — Google Fonts CSS + woff2 files
  if (/fonts\.(googleapis|gstatic)\.com/i.test(url)) return true;
  return false;
}

function isApiRequest(url) {
  // Only cache safe GETs against the known API origin
  return /pushkara\.onrender\.com\//i.test(url);
}

async function trimCache(cacheName, maxItems) {
  try {
    const cache = await caches.open(cacheName);
    const keys = await cache.keys();
    if (keys.length <= maxItems) return;
    // Oldest-first eviction (Cache API preserves insertion order)
    const overflow = keys.length - maxItems;
    for (let i = 0; i < overflow; i++) await cache.delete(keys[i]);
  } catch (e) { /* best-effort */ }
}

// ─── FETCH ROUTING ──────────────────────────────────────────────────────────
self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return; // never intercept writes
  const url = req.url;

  // Tiles — stale-while-revalidate
  if (isTileRequest(url)) {
    event.respondWith(
      caches.open(TILE_CACHE).then(async (cache) => {
        const cached = await cache.match(req);
        const network = fetch(req).then((res) => {
          if (res && res.status === 200) {
            cache.put(req, res.clone()).then(() => trimCache(TILE_CACHE, TILE_MAX_ITEMS));
          }
          return res;
        }).catch(() => null);
        return cached || network || new Response('', { status: 504 });
      })
    );
    return;
  }

  // App shell — cache-first
  if (isShellRequest(url)) {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req).then((res) => {
        if (res && res.status === 200 && res.type !== 'opaque') {
          const copy = res.clone();
          caches.open(SHELL_CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      }).catch(() => caches.match('/user'))) // hard offline → return cached app shell
    );
    return;
  }

  // API GETs — network-first with cache fallback
  if (isApiRequest(url)) {
    event.respondWith(
      fetch(req).then((res) => {
        if (res && res.status === 200) {
          const copy = res.clone();
          caches.open(API_CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      }).catch(async () => {
        const cached = await caches.match(req);
        if (cached) {
          // Add a header to let the page know we served stale data
          const headers = new Headers(cached.headers);
          headers.set('X-Pushkara-Stale', '1');
          return new Response(await cached.blob(), {
            status: cached.status,
            statusText: cached.statusText,
            headers,
          });
        }
        return new Response(
          JSON.stringify({ offline: true, error: 'No cached response available' }),
          { status: 503, headers: { 'Content-Type': 'application/json' } }
        );
      })
    );
    return;
  }

  // Navigation requests — try network, fall back to cached /user shell
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match('/user') || caches.match(req))
    );
    return;
  }

  // Everything else — pass through
});

// ─── MESSAGE — allow page to trigger immediate activation ───────────────────
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});
