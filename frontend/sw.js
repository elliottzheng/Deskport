// App 壳 Service Worker：壳资源离线兜底，API / 登录 / WS 一律不缓存
const CACHE = 'wp-shell-v2';
const PRECACHE = [
    '/app',
    '/assets/icon-192.png',
    '/assets/icon-512.png',
    '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const req = event.request;
    if (req.method !== 'GET') { return; }
    const url = new URL(req.url);
    if (url.origin !== self.location.origin) { return; }
    if (url.pathname.startsWith('/api/') || url.pathname === '/login'
        || url.pathname === '/logout' || url.pathname === '/sw.js') { return; }
    if (url.pathname.startsWith('/assets/')) {
        // 构建戳 (?v=) 已经保证内容唯一, 静态资源缓存优先, 避免每次切页重新下载
        event.respondWith(
            caches.match(req).then((hit) => hit || fetch(req).then((res) => {
                if (res.ok) {
                    const copy = res.clone();
                    caches.open(CACHE).then((c) => c.put(req, copy));
                }
                return res;
            }))
        );
        return;
    }
    // 其它：网络优先，失败回缓存（离线仍能打开壳）
    event.respondWith(
        fetch(req).then((res) => {
            if (res.ok && (req.mode === 'navigate' || url.pathname.startsWith('/assets/'))) {
                const copy = res.clone();
                caches.open(CACHE).then((c) => c.put(req, copy));
            }
            return res;
        }).catch(() => caches.match(req).then((hit) => hit || caches.match('/app')))
    );
});