/** 伸長（zlib / xz）と SHA-256。 */
import { XzReadableStream } from "xz-decompress";
import type { Compression } from "./meta";

export async function sha256Hex(data: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data as BufferSource);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function streamOf(data: Uint8Array): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      controller.enqueue(data);
      controller.close();
    },
  });
}

/** ストリームを読み切る。maxSize を超えたら中止する（伸長爆弾対策）。 */
async function readAll(stream: ReadableStream<Uint8Array>, maxSize: number): Promise<Uint8Array> {
  const reader = stream.getReader();
  const parts: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > maxSize) {
      await reader.cancel();
      throw new Error("伸長後のサイズが想定を超えています");
    }
    parts.push(value);
  }
  const out = new Uint8Array(size);
  let off = 0;
  for (const p of parts) {
    out.set(p, off);
    off += p.length;
  }
  return out;
}

/** Python 版 packer.decompress と同じ: none / zlib（zlib 形式）/ lzma（xz 形式）。 */
export async function decompress(method: Compression, payload: Uint8Array, maxSize: number): Promise<Uint8Array> {
  if (method === "none") return payload;
  if (method === "zlib") {
    // DecompressionStream の "deflate" は zlib 形式（ヘッダーとチェックサム付き）
    const s = streamOf(payload).pipeThrough(new DecompressionStream("deflate") as unknown as TransformStream<Uint8Array, Uint8Array>);
    return readAll(s, maxSize);
  }
  if (method === "lzma") return readAll(new XzReadableStream(streamOf(payload)), maxSize);
  throw new Error(`未対応の圧縮形式: ${method}`);
}
