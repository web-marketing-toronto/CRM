// Network-first service worker: the app is always live data, but the shell and an
// offline page stay available when a job site has no signal.
const VERSION = 'v2';
const SHELL = ['/static/offline.html', '/static/icon.svg'];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.pathname.startsWith('/static/')) {
    // Stale-while-revalidate: answer from cache for speed, but always refetch so the next load has the
    // current file. Versioned URLs (?v=hash) change whenever the file does, so a stale hit is short-lived.
    e.respondWith(caches.open(VERSION).then((c) => c.match(req).then((hit) => {
      const refresh = fetch(req).then((res) => { if (res.ok) c.put(req, res.clone()); return res; }).catch(() => hit);
      return hit || refresh;
    })));
    return;
  }
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match('/static/offline.html')));
  }
});
