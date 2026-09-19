/**
 * 転送プロトコル v1（Python 版 src/qrtransfer/protocol.py と同じ形式）。
 *
 * フレーム（ビッグエンディアン）:
 *   magic(2) "QZ" | version(1) | type(1) | session_id(4) | seq(4) | total(4) | payload(N) | crc32(4)
 * CRC32 はオフセット 0 から payload 末尾までを対象とする。
 * type: 0=META、1=DATA、2=REPAIR（修復用。seq は修復用フレームの番号。repair.ts を参照）
 */
import { crc32 } from "./crc32";

export const TYPE_META = 0;
export const TYPE_DATA = 1;
export const TYPE_REPAIR = 2;
export const HEADER_SIZE = 16;
export const CRC_SIZE = 4;
export const OVERHEAD = HEADER_SIZE + CRC_SIZE;
const VERSION = 1;
const SUPPORTED_VERSIONS = new Set([VERSION]);
const U32_MAX = 0xffffffff;

/** DATA 20 枚ごとに META を 1 枚挟む（Python 版 protocol.META_INTERVAL） */
export const META_INTERVAL = 20;

export interface Frame {
  type: number;
  sessionId: number;
  seq: number;
  total: number;
  payload: Uint8Array;
}

/** フレームを解析する。不正なフレームは null（黙って破棄）。 */
export function parseFrame(data: Uint8Array | null | undefined): Frame | null {
  if (!data || data.length < OVERHEAD) return null;
  if (data[0] !== 0x51 || data[1] !== 0x5a) return null; // "QZ"
  if (!SUPPORTED_VERSIONS.has(data[2])) return null;
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const crc = view.getUint32(data.length - CRC_SIZE);
  if (crc32(data, 0, data.length - CRC_SIZE) !== crc) return null;
  const type = data[3];
  if (type !== TYPE_META && type !== TYPE_DATA && type !== TYPE_REPAIR) return null;
  const sessionId = view.getUint32(4);
  const seq = view.getUint32(8);
  const total = view.getUint32(12);
  if (type === TYPE_DATA && seq >= total) return null;
  return { type, sessionId, seq, total, payload: data.slice(HEADER_SIZE, data.length - CRC_SIZE) };
}

/** フレームを組み立てる（Python 版 protocol.build_frame と同じバイト列になる）。 */
export function buildFrame(type: number, sessionId: number, seq: number, total: number, payload: Uint8Array): Uint8Array {
  if (type !== TYPE_META && type !== TYPE_DATA && type !== TYPE_REPAIR) throw new RangeError(`unknown frame type: ${type}`);
  for (const [name, v] of [["sessionId", sessionId], ["seq", seq], ["total", total]] as const) {
    if (!(Number.isInteger(v) && v >= 0 && v <= U32_MAX)) throw new RangeError(`${name} out of range: ${v}`);
  }
  const out = new Uint8Array(OVERHEAD + payload.length);
  const view = new DataView(out.buffer);
  out[0] = 0x51; // "Q"
  out[1] = 0x5a; // "Z"
  out[2] = VERSION;
  out[3] = type;
  view.setUint32(4, sessionId);
  view.setUint32(8, seq);
  view.setUint32(12, total);
  out.set(payload, HEADER_SIZE);
  view.setUint32(out.length - CRC_SIZE, crc32(out, 0, out.length - CRC_SIZE));
  return out;
}

// 全角の数字・記号も受け付ける（手入力のため。Python 版 protocol._NORMALIZE と同じ）
const NORMALIZE: Record<string, string> = Object.fromEntries(
  [..."０１２３４５６７８９，、－ー―‐〜~　"].map((c, i) => [c, "0123456789,,------ "[i]]),
);

/**
 * "1,3-5,7" → {1,3,4,5,7}（Python 版 protocol.parse_ranges と同じ）。
 * limit を指定すると limit 以上の値を拒否する。空文字列は空集合。不正な入力は Error。
 */
export function parseRanges(text: string, limit?: number): Set<number> {
  const s = [...text].map((c) => NORMALIZE[c] ?? c).join("").replace(/[\r\n]/g, ",");
  const result = new Set<number>();
  if (!s.trim()) return result;
  for (const raw of s.split(",")) {
    const item = raw.replace(/^[ \t]+|[ \t]+$/g, "");
    if (!item) throw new Error(`空の項目があります: ${text}`);
    const m = /^(\d+)(?:\s*-\s*(\d+))?$/.exec(item);
    if (!m) throw new Error(`番号として読めません: ${raw}`);
    const start = Number(m[1]);
    const end = m[2] !== undefined ? Number(m[2]) : start;
    if (end < start) throw new Error(`範囲が逆です: ${raw}`);
    if (limit !== undefined && end >= limit) throw new Error(`番号が大きすぎます（${limit - 1} まで）: ${raw}`);
    if (end > U32_MAX) throw new Error(`番号が大きすぎます: ${raw}`);
    if (end - start > 10_000_000) throw new Error(`範囲が広すぎます: ${raw}`);
    for (let v = start; v <= end; v++) result.add(v);
  }
  return result;
}

/** 昇順の [start, end] 区間列（end を含む）。 */
export function* iterRanges(values: Iterable<number>): Generator<[number, number]> {
  const sorted = [...new Set(values)].sort((a, b) => a - b);
  let start: number | null = null;
  let prev = 0;
  for (const v of sorted) {
    if (start === null) {
      start = prev = v;
    } else if (v === prev + 1) {
      prev = v;
    } else {
      yield [start, prev];
      start = prev = v;
    }
  }
  if (start !== null) yield [start, prev];
}

/** [1,3,4,5,7] → "1,3-5,7"。maxItems を超える分は ",…" にまとめる（表示用）。 */
export function formatRanges(values: Iterable<number>, maxItems?: number): string {
  const parts: string[] = [];
  let i = 0;
  for (const [s, e] of iterRanges(values)) {
    if (maxItems !== undefined && i >= maxItems) {
      parts.push("…");
      break;
    }
    parts.push(s === e ? String(s) : `${s}-${e}`);
    i++;
  }
  return parts.join(",");
}

export function hex32(n: number): string {
  return (n >>> 0).toString(16).padStart(8, "0");
}
