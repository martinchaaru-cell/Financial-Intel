// Finance Intel service worker.
// Deliberately light: caches static assets only. Pages and /api/ data always go to the
// network so financial figures are never served stale. Offline shows a friendly message.
const CACHE = 'finance-intel-static-v2';
const SHELL = '/__shell';   // the last good copy of the app page (header + navigation)
const PRECACHE = [
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png'
];

const OFFLINE_HTML = `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Offline | Finance Intel</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0a0f1c;color:#f5f7fc;font-family:system-ui,sans-serif;text-align:center;padding:24px}
h1{font-size:20px;margin:0 0 8px}p{color:#aab4cc;margin:0 0 20px}
button{background:#5b9bff;color:#0a0f1c;border:0;border-radius:8px;padding:10px 20px;font-size:15px;font-weight:600}</style>
</head><body><div><h1>You're offline</h1><p>Finance Intel needs a connection to load live data.</p>
<button onclick="location.reload()">Try again</button></div></body></html>`;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/')) return;            // live data: never cache

  if (req.mode === 'navigate') {                           // pages: network first, cached shell when the server is down
    e.respondWith((async () => {
      try {
        const res = await fetch(req);
        // Only our own app sets this header, so a host's "app is not running" page is never mistaken for the app.
        if (res.ok && res.headers.get('X-Finance-Intel') === '1') {
          if (url.pathname === '/') { const c = await caches.open(CACHE); await c.put(SHELL, res.clone()); }
          return res;
        }
        if (res.headers.get('X-Finance-Intel') === '1') return res;     // a real app error page (e.g. 403)
      } catch (err) { /* network down */ }
      const cached = await (await caches.open(CACHE)).match(SHELL);
      return cached || new Response(OFFLINE_HTML, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
    })());
    return;
  }

  if (url.pathname.startsWith('/static/')) {               // static: stale-while-revalidate
    e.respondWith(caches.open(CACHE).then(async cache => {
      const hit = await cache.match(req);
      const net = fetch(req).then(res => { if (res.ok) cache.put(req, res.clone()); return res; }).catch(() => hit);
      return hit || net;
    }));
  }
});