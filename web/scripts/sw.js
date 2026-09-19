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
// 受信した動画・音声をページに渡すための置き場（src/lib/media.ts と同じ名前）。版ごとのキャッシュとは別に扱う
const MEDIA_CACHE = "received-media";
const MEDIA_PREFIX = new URL("received-media/", self.registration.scope).href;

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
  // 開こうとしたページ（受信・送信）を返す。見つからなければ受信ページ
  return (
    (await cache.match(request, { ignoreSearch: true, ignoreVary: true })) ??
    (await cache.match(INDEX, { ignoreVary: true })) ??
    Response.error()
  );
}

/**
 * 受信した動画・音声を、範囲指定（Range）に対応した普通の URL として返す。
 * iOS の Safari は blob: の URL や範囲指定に応じない URL の動画を再生しないことがあるため。
 */
async function media(request) {
  const hit = await (await caches.open(MEDIA_CACHE)).match(request.url);
  if (!hit) return new Response("Not Found", { status: 404 });
  const blob = await hit.blob();
  const headers = {
    "Content-Type": hit.headers.get("Content-Type") || "application/octet-stream",
    "Accept-Ranges": "bytes",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox", // 直接開かれてもページとしては動かさない
    "Cache-Control": "no-store",
  };
  const range = /^bytes=(\d*)-(\d*)$/.exec(request.headers.get("Range") || "");
  if (!range) return new Response(blob, { headers: { ...headers, "Content-Length": String(blob.size) } });
  let start;
  let end;
  if (range[1] === "") {
    // 末尾から n バイト
    start = Math.max(0, blob.size - Number(range[2]));
    end = blob.size - 1;
  } else {
    start = Number(range[1]);
    end = range[2] === "" ? blob.size - 1 : Math.min(Number(range[2]), blob.size - 1);
  }
  if (range[1] === "" && range[2] === "" || start >= blob.size || start > end) {
    return new Response(null, { status: 416, headers: { ...headers, "Content-Range": `bytes */${blob.size}` } });
  }
  return new Response(blob.slice(start, end + 1), {
    status: 206,
    headers: { ...headers, "Content-Length": String(end - start + 1), "Content-Range": `bytes ${start}-${end}/${blob.size}` },
  });
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
  if (request.url.startsWith(MEDIA_PREFIX)) event.respondWith(media(request));
  else event.respondWith(request.mode === "navigate" ? navigate(request) : asset(request));
});
