// Remuneration Practice service worker.
// Deliberately light: caches static assets only. Pages and /api/ data always go to the
// network so financial figures are never served stale. Offline shows a friendly message.
const CACHE = 'remuneration-practice-static-v13-comparison6';
const PRECACHE = [
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
  '/static/vendors/chart.umd.min.js'
];
// How long the installed app waits for the server before showing the last saved copy of the app (or the
// "back shortly" page, which reloads by itself). Without a limit, a server that is asleep, waking up or
// stuck left the installed app on its splash screen indefinitely.
const PAGE_TIMEOUT_MS = 8000;

// Shown when the app page cannot be reached. Usually the server is restarting or being updated, so the page says
// so (rather than blaming the visitor's internet) and reloads itself as soon as the app answers again. Only when
// the device itself reports no connection does it tell the visitor to check their internet.
const OFFLINE_HTML = `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Back shortly | Remuneration Practice</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#101c18;color:#f5fcf9;font-family:system-ui,sans-serif;text-align:center;padding:24px}
.card{max-width:420px}.icon{font-size:40px;margin-bottom:12px}
h1{font-size:21px;margin:0 0 10px}p{color:#aaccbb;margin:0 0 8px;line-height:1.5}
.note{font-size:13px;color:#7fa391;margin:16px 0 20px}
button{background:#48cfa0;color:#101c18;border:0;border-radius:8px;padding:10px 22px;font-size:15px;font-weight:600;cursor:pointer}</style>
</head><body><div class="card">
<div class="icon" id="icon">&#128295;</div>
<h1 id="title">We'll be back shortly</h1>
<p id="msg">Remuneration Practice is undergoing maintenance to improve your experience. Your data is safe.</p>
<p class="note" id="note">This page will reload automatically once we're back online.</p>
<button onclick="location.reload()">Try again</button></div>
<script>
function show(){
  if(!navigator.onLine){
    document.getElementById('icon').innerHTML='&#128246;';
    document.getElementById('title').textContent='No internet connection';
    document.getElementById('msg').textContent='Please check your connection. Remuneration Practice needs internet access to load live data.';
    document.getElementById('note').textContent='This page will reload automatically when your connection returns.';
  } else {
    document.getElementById('icon').innerHTML='&#128295;';
    document.getElementById('title').textContent="We'll be back shortly";
    document.getElementById('msg').textContent='Remuneration Practice is undergoing maintenance to improve your experience. Your data is safe.';
    document.getElementById('note').textContent="This page will reload automatically once we're back online.";
  }
}
show();
window.addEventListener('online',show);window.addEventListener('offline',show);
setInterval(function(){
  if(!navigator.onLine)return;
  fetch('/',{cache:'no-store'}).then(function(r){if(r.ok&&r.headers.get('X-Finance-Intel')==='1')location.reload();}).catch(function(){});
},20000);
</script></body></html>`;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE && (k.startsWith('remuneration-practice-static-') || k.startsWith('finance-intel-static-'))).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/')) return;            // live data: never cache

  // Pages shown inside a frame (e.g. Replit's preview pane) always go straight to the network. Otherwise a brief
  // server restart left the preview stuck on the offline page, because the frame never reloads the real app page.
  if (req.mode === 'navigate' && req.destination !== 'document') return;

  if (req.mode === 'navigate') {                           // pages: network only, offline notice when unavailable
    e.respondWith((async () => {
      try {
        const network = fetch(req);
        // Authenticated HTML is never stored by this worker.

        const res = await network;
        // A redirect (e.g. to the sign-in page after the person was signed out) is passed straight to the browser.
        // Page requests do not follow redirects inside the service worker, so they arrive here as "opaqueredirect";
        // treating that as "server down" showed the maintenance page to anyone who was signed out.
        if (res.type === 'opaqueredirect' || (res.status >= 300 && res.status < 400)) return res;
        // Only our own app sets this header, so a host's "app is not running" page is never mistaken for the app.
        if (res.ok && res.headers.get('X-Finance-Intel') === '1') return res;
        if (res.headers.get('X-Finance-Intel') === '1') return res;     // a real app error page (e.g. 403)
      } catch (err) { /* network down */ }
      return new Response(OFFLINE_HTML, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
    })());
    return;
  }

  if (url.pathname === '/static/app-runtime.js' && url.search === '?v=comparison6' || PRECACHE.includes(url.pathname) && !url.search) { // release-versioned static assets only
    e.respondWith(caches.open(CACHE).then(async cache => {
      const hit = await cache.match(req);
      if (hit) return hit;
      const res = await fetch(req);
      if (res.ok) await cache.put(req, res.clone());
      return res;
    }));
  }
});