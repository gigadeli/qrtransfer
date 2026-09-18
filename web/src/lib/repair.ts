/**
 * 修復用フレーム（REPAIR）: 複数のチャンクを XOR で重ねたもの（Python 版 src/qrtransfer/repair.py と同じ決まり）。
 * 受け取った DATA と REPAIR の枚数が全チャンク数を少し超えた時点で、欠けたチャンクを計算で求められる。
 *
 * どのチャンクを重ねるかは session_id と修復用フレームの番号から、整数演算だけで決まる（Python 版と必ず一致する）。
 */

export const DEGREE_MAX = 1024;
export const TOTAL_LIMIT = 2 ** 21; // 番号の計算（next() * n）を 2^53 未満に収めるため
const MEMORY_LIMIT = 64 * 1024 * 1024; // まだ解けていない式を保持する上限（バイト）

export function degree(total: number): number {
  return Math.max(1, Math.min(total, DEGREE_MAX, Math.floor((total + 1) / 2)));
}

class Mulberry32 {
  private a: number;

  constructor(seed: number) {
    this.a = seed >>> 0;
  }

  next(): number {
    this.a = (this.a + 0x6d2b79f5) >>> 0;
    let t = this.a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return (t ^ (t >>> 14)) >>> 0;
  }

  below(n: number): number {
    return Math.floor((this.next() * n) / 4294967296);
  }
}

/** index 番目の修復用フレームが重ねるチャンクの番号（昇順）。 */
export function repairIndices(sessionId: number, index: number, total: number): number[] {
  if (!(total > 0 && total < TOTAL_LIMIT)) throw new RangeError(`total out of range: ${total}`);
  const rng = new Mulberry32((sessionId ^ Math.imul(index + 1, 0x9e3779b1)) >>> 0);
  const d = degree(total);
  const chosen = new Set<number>();
  // Floyd の方法: total 個から d 個を重複なく選ぶ
  for (let j = total - d; j < total; j++) {
    const t = rng.below(j + 1);
    chosen.add(chosen.has(t) ? j : t);
  }
  return [...chosen].sort((x, y) => x - y);
}

interface Row {
  bits: Uint32Array; // 未知数（まだ受け取っていないチャンク）のビット列
  data: Uint8Array;
}

function xorInto(dst: Uint32Array, src: Uint32Array): void {
  for (let i = 0; i < dst.length; i++) dst[i] ^= src[i];
}

/** dst ^= src（src の長さまで）。両方が 4 バイト境界にあれば 4 バイトずつ計算する（1 バイトずつの約 4 倍速い）。 */
function xorBytes(dst: Uint8Array, src: Uint8Array): void {
  const n = Math.min(dst.length, src.length);
  let i = 0;
  if (dst.byteOffset % 4 === 0 && src.byteOffset % 4 === 0) {
    const words = n >>> 2;
    const d = new Uint32Array(dst.buffer, dst.byteOffset, words);
    const s = new Uint32Array(src.buffer, src.byteOffset, words);
    for (let k = 0; k < words; k++) d[k] ^= s[k];
    i = words * 4;
  }
  for (; i < n; i++) dst[i] ^= src[i];
}

function lowestBit(bits: Uint32Array): number {
  for (let w = 0; w < bits.length; w++) {
    const v = bits[w];
    if (v) return w * 32 + (31 - Math.clz32(v & -v));
  }
  return -1;
}

function isSingle(bits: Uint32Array): boolean {
  let seen = false;
  for (let w = 0; w < bits.length; w++) {
    const v = bits[w];
    if (!v) continue;
    if (seen || v & (v - 1)) return false;
    seen = true;
  }
  return seen;
}

/**
 * 修復用フレームの式を集め、解けたチャンクを返す（Python 版 repair.Decoder と同じ手順）。
 * 式は既約行階段形（各行の先頭の未知数は他の行に現れない）で持ち、未知数が 1 つになった行が「解けた」チャンク。
 */
export class RepairDecoder {
  private rows = new Map<number, Row>();
  private readonly words: number;
  readonly maxRows: number;
  dropped = 0;

  constructor(
    readonly total: number,
    readonly length: number,
    memoryLimit = MEMORY_LIMIT,
  ) {
    this.words = Math.ceil(total / 32);
    this.maxRows = Math.max(1, Math.floor(memoryLimit / (this.words * 4 + length + 64)));
  }

  get pending(): number {
    return this.rows.size;
  }

  private padded(data: Uint8Array): Uint8Array {
    const out = new Uint8Array(this.length);
    out.set(data.subarray(0, this.length));
    return out;
  }

  /** 修復用フレーム 1 枚分の式を加える。known(i) は受信済みのチャンク。解けたチャンクを返す。 */
  addRepair(indices: number[], payload: Uint8Array, known: (i: number) => Uint8Array | undefined): [number, Uint8Array][] {
    const bits = new Uint32Array(this.words);
    const data = this.padded(payload);
    for (const i of indices) {
      const c = known(i);
      if (c) {
        xorBytes(data, c);
      } else {
        bits[i >>> 5] |= 1 << (i & 31);
      }
    }
    return this.insert(bits, data, -1);
  }

  /** DATA で受け取ったチャンクを式に反映する。これによって解けた（ほかの）チャンクを返す。 */
  addKnown(seq: number, chunk: Uint8Array): [number, Uint8Array][] {
    const w = seq >>> 5;
    const m = 1 << (seq & 31);
    let used = false;
    for (const r of this.rows.values()) {
      if (r.bits[w] & m) {
        used = true;
        break;
      }
    }
    if (!used) return [];
    const bits = new Uint32Array(this.words);
    bits[w] = m;
    return this.insert(bits, this.padded(chunk), seq);
  }

  private insert(bits: Uint32Array, data: Uint8Array, skip: number): [number, Uint8Array][] {
    // 既にある行の先頭の未知数を消す（各行の先頭は他の行に現れないので、元の式の位置だけ見ればよい）
    const orig = bits.slice();
    for (let w = 0; w < orig.length; w++) {
      let v = orig[w];
      while (v) {
        const low = v & -v;
        v ^= low;
        const row = this.rows.get(w * 32 + (31 - Math.clz32(low)));
        if (row) {
          xorInto(bits, row.bits);
          xorBytes(data, row.data);
        }
      }
    }
    const pivot = lowestBit(bits);
    if (pivot < 0) return []; // ほかの式から導ける
    if (this.rows.size >= this.maxRows && !isSingle(bits)) {
      this.dropped++;
      return [];
    }
    const w = pivot >>> 5;
    const m = 1 << (pivot & 31);
    const touched = [pivot];
    for (const [q, r] of this.rows) {
      if (r.bits[w] & m) {
        xorInto(r.bits, bits);
        xorBytes(r.data, data);
        touched.push(q);
      }
    }
    this.rows.set(pivot, { bits, data });
    const solved: [number, Uint8Array][] = [];
    for (const q of touched) {
      const r = this.rows.get(q)!;
      if (isSingle(r.bits)) {
        this.rows.delete(q);
        if (q !== skip) solved.push([q, r.data]);
      }
    }
    return solved;
  }
}
