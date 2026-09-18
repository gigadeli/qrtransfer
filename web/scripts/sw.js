/*
 * オフライン用の Service Worker（ビルド時に vite.config.ts の offline() が dist/sw.js として書き出す）。
 * 一度開いたときにページ・読み取り部品（WebAssembly）・アイコンをすべて端末に保存し、以後は通信なしで開けるようにする。
 * VERSION と FILES の値はビルド時に書き込まれる。
 */
const VERSION = __VERSION__;
const FILES = __FILES__;
const CACHE = `qrtransfer-${VERSION}`;
const INDEX = new URL("./", self.registration.scope).href;
const NAV_TIMEOUT_MS = 3000;

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE);
      // HTTP キャッシュの古い中身を保存しないように、必ず取り直す
      await cache.addAll(FILES.map((f) => new Request(new URL(f, self.registration.scope), { cache: "reload" })));
      await cache.put(INDEX, (await cache.match(new URL("index.html", self.registration.scope))).clone());
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // 1 つ前の版は残す（更新前に開いたページが、あとから読み取り部品などを読み込めるように）
      const old = (await caches.keys()).filter((k) => k.startsWith("qrtransfer-") && k !== CACHE);
      for (const key of old.slice(0, -1)) await caches.delete(key);
      await self.clients.claim();
    })(),
  );
});

/** ページ本体は通信を優先（つながっていれば最新版を開く）。つながらない・遅いときは保存したものを使う。 */
async function navigate(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await Promise.race([
      fetch(request),
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), NAV_TIMEOUT_MS)),
    ]);
    if (response.ok) return response;
  } catch {
    /* オフライン */
  }
  return (await cache.match(INDEX, { ignoreVary: true })) ?? Response.error();
}

/** それ以外（名前にハッシュが付いたファイル）は保存したものを優先する。 */
async function asset(request) {
  // Vary（Origin など）は見ない。crossorigin 付きの script は Origin ヘッダーを送るので、保存時の要求と一致しなくなる
  const hit = await caches.match(request, { ignoreVary: true });
  if (hit) return hit;
  return fetch(request);
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET" || !request.url.startsWith(self.registration.scope)) return;
  event.respondWith(request.mode === "navigate" ? navigate(request) : asset(request));
});
