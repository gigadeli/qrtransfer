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
const SUPPORTED_VERSIONS = new Set([1]);

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
