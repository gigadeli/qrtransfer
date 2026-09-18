/** 伸長（zlib / xz）と SHA-256。 */
import { Unzlib } from "fflate";
import { XzReadableStream } from "xz-decompress";
import type { Compression } from "./meta";

const SLICE = 64 * 1024;

export class TooLargeError extends Error {
  constructor() {
    super("伸長後のサイズが想定を超えています");
  }
}

export async function sha256Hex(data: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data as BufferSource);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** 伸長した断片を maxSize まで受け取り、超えたら TooLargeError（伸長爆弾で端末のメモリを使い切らないように）。 */
class Collector {
  private parts: Uint8Array[] = [];
  size = 0;

  constructor(private readonly maxSize: number) {}

  add(chunk: Uint8Array): void {
    this.size += chunk.length;
    if (this.size > this.maxSize) {
      this.parts = [];
      throw new TooLargeError();
    }
    this.parts.push(chunk);
  }

  result(): Uint8Array {
    if (this.parts.length === 1) return this.parts[0];
    const out = new Uint8Array(this.size);
    let off = 0;
    for (const p of this.parts) {
      out.set(p, off);
      off += p.length;
    }
    this.parts = [];
    return out;
  }
}

function inflateZlib(payload: Uint8Array, maxSize: number): Uint8Array {
  // ブラウザの DecompressionStream は iOS 16.4 未満に無いので、fflate で伸長する（少しずつ入れて量を数える）
  const out = new Collector(maxSize);
  let ended = false;
  const z = new Unzlib((chunk, final) => {
    out.add(chunk);
    if (final) ended = true;
  });
  for (let off = 0; off < payload.length; off += SLICE) {
    const end = Math.min(payload.length, off + SLICE);
    z.push(payload.subarray(off, end), end === payload.length);
  }
  if (!ended) throw new Error("zlib データが途中で終わっています");
  return out.result();
}

async function inflateXz(payload: Uint8Array, maxSize: number): Promise<Uint8Array> {
  const input = new ReadableStream<Uint8Array>({
    start(controller) {
      for (let off = 0; off < payload.length; off += SLICE) controller.enqueue(payload.subarray(off, off + SLICE));
      controller.close();
    },
  });
  const reader = new XzReadableStream(input).getReader();
  const out = new Collector(maxSize);
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      out.add(value);
    }
  } catch (e) {
    await reader.cancel().catch(() => undefined);
    throw e;
  }
  return out.result();
}

/**
 * Python 版 packer.decompress と同じ: none / zlib（zlib 形式）/ lzma（xz 形式）。
 * maxSize を超える伸長は途中で打ち切る。
 */
export async function decompress(method: Compression, payload: Uint8Array, maxSize: number): Promise<Uint8Array> {
  if (method === "none") {
    if (payload.length > maxSize) throw new TooLargeError();
    return payload;
  }
  if (method === "zlib") return inflateZlib(payload, maxSize);
  if (method === "lzma") return inflateXz(payload, maxSize);
  throw new Error(`未対応の圧縮形式: ${method}`);
}
