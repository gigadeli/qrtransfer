/**
 * 受信した動画・音声を、Service Worker 経由の普通の URL で再生できるようにする（scripts/sw.js の media()）。
 * iOS の Safari は blob: の URL の動画・音声を安定して再生しないため、データをキャッシュに入れて
 * 範囲指定（Range）に応じる URL から読ませる。Service Worker が動いていないときは null（blob: の URL を使う）。
 */

const MEDIA_CACHE = "received-media"; // scripts/sw.js と同じ名前

/** 前回までに残った受信データを消す（タブを閉じたときなどに消し残したもの）。 */
export async function clearMedia(): Promise<void> {
  try {
    await caches.delete(MEDIA_CACHE);
  } catch {
    /* キャッシュが使えない環境 */
  }
}

/** データをキャッシュに入れ、再生用の URL と後片付けの関数を返す。使えない環境では null。 */
export async function putMedia(data: Uint8Array, name: string, mime: string): Promise<{ url: string; release: () => void } | null> {
  if (!("caches" in window) || !navigator.serviceWorker?.controller) return null;
  try {
    const url = new URL(
      `${import.meta.env.BASE_URL}received-media/${crypto.randomUUID()}/${encodeURIComponent(name)}`,
      location.href,
    ).href;
    const cache = await caches.open(MEDIA_CACHE);
    await cache.put(url, new Response(new Blob([data as BlobPart], { type: mime }), { headers: { "Content-Type": mime } }));
    return { url, release: () => void cache.delete(url).catch(() => undefined) };
  } catch {
    return null; // 容量不足など
  }
}
