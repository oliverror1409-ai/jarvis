// Installability needs a service worker with a fetch handler. This one is
// deliberately a pass-through: a worker that cached the HUD would keep
// serving yesterday's copy after a git pull, and that is a miserable bug to
// diagnose. Nothing here is stored, so an update is always the real file.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => { /* straight to the network */ });
