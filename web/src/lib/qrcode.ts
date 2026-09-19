/**
 * QR コードの生成（バイトモードのみ）。送信側で、決まったバージョンと誤り訂正レベルの QR を作る。
 *
 * 転送では全フレームを同じバージョンにそろえ、誤り訂正レベルを勝手に上げないことが必要なので、
 * 汎用のライブラリではなく、JIS X 0510（ISO/IEC 18004）の手順をそのまま実装している。
 * 読み取れることは、受信側の読み取り部（zxing-wasm）で読み戻すテストで確かめている。
 */

export type Ecc = "l" | "m" | "q" | "h";

const ECC_ORDER: Record<Ecc, number> = { l: 0, m: 1, q: 2, h: 3 };
const FORMAT_BITS: Record<Ecc, number> = { l: 1, m: 0, q: 3, h: 2 };

// 誤り訂正のブロック 1 つあたりの誤り訂正コード語数と、ブロック数（添字はバージョン。0 は使わない）
// prettier-ignore
const ECC_CODEWORDS_PER_BLOCK: number[][] = [
  [-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
  [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28],
  [-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
  [-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
];
// prettier-ignore
const NUM_ERROR_CORRECTION_BLOCKS: number[][] = [
  [-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25],
  [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49],
  [-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68],
  [-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81],
];

export const MIN_VERSION = 1;
export const MAX_VERSION = 40;

/** 1 辺のモジュール数（周囲の余白を含まない） */
export function symbolSize(version: number): number {
  return version * 4 + 17;
}

/** データとして使えるモジュール数（機能パターン・形式情報・型番情報を除く） */
function numRawDataModules(version: number): number {
  let result = (16 * version + 128) * version + 64;
  if (version >= 2) {
    const numAlign = Math.floor(version / 7) + 2;
    result -= (25 * numAlign - 10) * numAlign - 55;
    if (version >= 7) result -= 36;
  }
  return result;
}

/** データのコード語数（誤り訂正コード語を除く） */
function numDataCodewords(version: number, ecc: Ecc): number {
  const e = ECC_ORDER[ecc];
  return Math.floor(numRawDataModules(version) / 8) - ECC_CODEWORDS_PER_BLOCK[e][version] * NUM_ERROR_CORRECTION_BLOCKS[e][version];
}

/** バイトモードで入る最大バイト数（Python 版 qrgen.capacity と同じ値） */
export function capacity(version: number, ecc: Ecc): number {
  if (!(version >= MIN_VERSION && version <= MAX_VERSION)) throw new RangeError(`invalid version: ${version}`);
  const cci = version < 10 ? 8 : 16;
  return Math.floor((numDataCodewords(version, ecc) * 8 - 4 - cci) / 8);
}

// ---------------------------------------------------------------------------
// Reed-Solomon（GF(2^8)、生成多項式 0x11D）
// ---------------------------------------------------------------------------

const EXP = new Uint8Array(512);
const LOG = new Uint8Array(256);
(() => {
  let x = 1;
  for (let i = 0; i < 255; i++) {
    EXP[i] = x;
    LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
})();

function gfMul(a: number, b: number): number {
  return a && b ? EXP[LOG[a] + LOG[b]] : 0;
}

const divisors = new Map<number, Uint8Array>();

function rsDivisor(degree: number): Uint8Array {
  let d = divisors.get(degree);
  if (d) return d;
  d = new Uint8Array(degree);
  d[degree - 1] = 1;
  let root = 1;
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < degree; j++) {
      d[j] = gfMul(d[j], root);
      if (j + 1 < degree) d[j] ^= d[j + 1];
    }
    root = gfMul(root, 0x02);
  }
  divisors.set(degree, d);
  return d;
}

function rsRemainder(data: Uint8Array, divisor: Uint8Array): Uint8Array {
  const n = divisor.length;
  const result = new Uint8Array(n);
  for (const b of data) {
    const factor = b ^ result[0];
    result.copyWithin(0, 1);
    result[n - 1] = 0;
    if (factor) {
      const lf = LOG[factor];
      for (let i = 0; i < n; i++) if (divisor[i]) result[i] ^= EXP[LOG[divisor[i]] + lf];
    }
  }
  return result;
}

// ---------------------------------------------------------------------------
// コード語の組み立て
// ---------------------------------------------------------------------------

/** データのビット列（モード指示子・文字数・データ・終端・埋め草）をコード語にする */
function dataCodewords(data: Uint8Array, version: number, ecc: Ecc): Uint8Array {
  const cap = numDataCodewords(version, ecc);
  const cci = version < 10 ? 8 : 16;
  if (data.length > capacity(version, ecc)) throw new RangeError(`data too long for version ${version}-${ecc.toUpperCase()}: ${data.length}`);
  const out = new Uint8Array(cap);
  let bit = 0;
  const put = (value: number, len: number) => {
    for (let i = len - 1; i >= 0; i--, bit++) if ((value >>> i) & 1) out[bit >>> 3] |= 0x80 >>> (bit & 7);
  };
  put(0b0100, 4); // バイトモード
  put(data.length, cci);
  for (const b of data) put(b, 8);
  bit += Math.min(4, cap * 8 - bit); // 終端（0 のまま）
  bit = (bit + 7) & ~7;
  for (let pad = 0xec; bit < cap * 8; pad ^= 0xec ^ 0x11) put(pad, 8);
  return out;
}

/** ブロックに分けて誤り訂正コード語を付け、交互に並べる */
function interleave(data: Uint8Array, version: number, ecc: Ecc): Uint8Array {
  const e = ECC_ORDER[ecc];
  const numBlocks = NUM_ERROR_CORRECTION_BLOCKS[e][version];
  const eccLen = ECC_CODEWORDS_PER_BLOCK[e][version];
  const rawCodewords = Math.floor(numRawDataModules(version) / 8);
  const numShort = numBlocks - (rawCodewords % numBlocks);
  const shortLen = Math.floor(rawCodewords / numBlocks); // 短いブロックの長さ（誤り訂正を含む）
  const divisor = rsDivisor(eccLen);
  const blocks: { dat: Uint8Array; ecc: Uint8Array }[] = [];
  for (let i = 0, k = 0; i < numBlocks; i++) {
    const len = shortLen - eccLen + (i < numShort ? 0 : 1);
    const dat = data.subarray(k, k + len);
    k += len;
    blocks.push({ dat, ecc: rsRemainder(dat, divisor) });
  }
  const out = new Uint8Array(rawCodewords);
  let o = 0;
  const longData = shortLen - eccLen + 1;
  for (let i = 0; i < longData; i++) for (const b of blocks) if (i < b.dat.length) out[o++] = b.dat[i];
  for (let i = 0; i < eccLen; i++) for (const b of blocks) out[o++] = b.ecc[i];
  return out;
}

// ---------------------------------------------------------------------------
// モジュールの配置
// ---------------------------------------------------------------------------

/** モジュールを 1 ビットずつに詰める（8 分の 1 の大きさで受け渡し・保持するため） */
export function packModules(modules: Uint8Array): Uint8Array {
  const out = new Uint8Array((modules.length + 7) >>> 3);
  for (let i = 0; i < modules.length; i++) if (modules[i]) out[i >>> 3] |= 0x80 >>> (i & 7);
  return out;
}

export interface QrMatrix {
  version: number;
  size: number; // 1 辺のモジュール数（余白を含まない）
  modules: Uint8Array; // size*size。1 = 黒
}

function alignmentPositions(version: number): number[] {
  if (version === 1) return [];
  const size = symbolSize(version);
  const numAlign = Math.floor(version / 7) + 2;
  const step = Math.floor((version * 8 + numAlign * 3 + 5) / (numAlign * 4 - 4)) * 2;
  const result = [6];
  for (let pos = size - 7; result.length < numAlign; pos -= step) result.splice(1, 0, pos);
  return result;
}

function maskCondition(mask: number, x: number, y: number): boolean {
  switch (mask) {
    case 0: return (x + y) % 2 === 0;
    case 1: return y % 2 === 0;
    case 2: return x % 3 === 0;
    case 3: return (x + y) % 3 === 0;
    case 4: return (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0;
    case 5: return ((x * y) % 2) + ((x * y) % 3) === 0;
    case 6: return (((x * y) % 2) + ((x * y) % 3)) % 2 === 0;
    default: return (((x + y) % 2) + ((x * y) % 3)) % 2 === 0;
  }
}

// バージョンごとのマスクの模様（機能パターンの位置は 0）。毎回計算すると遅いので作り置く
const maskCache = new Map<number, Uint8Array[]>();

function maskPatterns(b: { version: number; size: number; fn: Uint8Array }): Uint8Array[] {
  let masks = maskCache.get(b.version);
  if (masks) return masks;
  const n = b.size;
  masks = Array.from({ length: 8 }, (_, mask) => {
    const p = new Uint8Array(n * n);
    for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) if (!b.fn[y * n + x] && maskCondition(mask, x, y)) p[y * n + x] = 1;
    return p;
  });
  maskCache.set(b.version, masks);
  return masks;
}

/** 1 行（または 1 列）の、同じ色の連続とファインダーに似た模様の減点 */
function linePenalty(m: Uint8Array, start: number, step: number, n: number): number {
  let score = 0;
  let run = 0;
  let prev = -1;
  let window = 0; // 直近 11 モジュールのビット列
  for (let b = 0, k = start; b < n; b++, k += step) {
    const v = m[k];
    if (v === prev) {
      run++;
      if (run === 5) score += 3;
      else if (run > 5) score++;
    } else {
      run = 1;
      prev = v;
    }
    window = ((window << 1) | v) & 0x7ff;
    if (b >= 10 && (window === 0b10111010000 || window === 0b00001011101)) score += 40;
  }
  return score;
}

const templates = new Map<number, { modules: Uint8Array; fn: Uint8Array; dataPos: Int32Array }>();

class Builder {
  readonly size: number;
  readonly modules: Uint8Array;
  readonly fn: Uint8Array; // 1 = 機能パターン（データを置かない・マスクしない）
  readonly dataPos: Int32Array;

  constructor(readonly version: number) {
    this.size = symbolSize(version);
    const t = templates.get(version);
    if (t) {
      this.modules = t.modules.slice();
      this.fn = t.fn;
      this.dataPos = t.dataPos;
      return;
    }
    this.modules = new Uint8Array(this.size * this.size);
    this.fn = new Uint8Array(this.size * this.size);
    this.drawFunctionPatterns();
    this.dataPos = this.dataPositions();
    templates.set(version, { modules: this.modules.slice(), fn: this.fn, dataPos: this.dataPos });
  }

  /** データを置くモジュールの位置（置く順）。右下から 2 列ずつジグザグに進む */
  private dataPositions(): Int32Array {
    const n = this.size;
    const out: number[] = [];
    for (let right = n - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      const upward = ((right + 1) & 2) === 0;
      for (let vert = 0; vert < n; vert++) {
        const y = upward ? n - 1 - vert : vert;
        for (let j = 0; j < 2; j++) {
          const k = y * n + right - j;
          if (!this.fn[k]) out.push(k);
        }
      }
    }
    return Int32Array.from(out);
  }

  private set(x: number, y: number, dark: boolean): void {
    const i = y * this.size + x;
    this.modules[i] = dark ? 1 : 0;
    this.fn[i] = 1;
  }

  private drawFunctionPatterns(): void {
    const n = this.size;
    for (let i = 0; i < n; i++) {
      this.set(6, i, i % 2 === 0);
      this.set(i, 6, i % 2 === 0);
    }
    for (const [cx, cy] of [[3, 3], [n - 4, 3], [3, n - 4]]) {
      for (let dy = -4; dy <= 4; dy++) {
        for (let dx = -4; dx <= 4; dx++) {
          const x = cx + dx;
          const y = cy + dy;
          if (x < 0 || x >= n || y < 0 || y >= n) continue;
          const dist = Math.max(Math.abs(dx), Math.abs(dy));
          this.set(x, y, dist !== 2 && dist !== 4);
        }
      }
    }
    const align = alignmentPositions(this.version);
    const last = align.length - 1;
    for (let i = 0; i < align.length; i++) {
      for (let j = 0; j < align.length; j++) {
        if ((i === 0 && j === 0) || (i === 0 && j === last) || (i === last && j === 0)) continue;
        for (let dy = -2; dy <= 2; dy++) {
          for (let dx = -2; dx <= 2; dx++) this.set(align[i] + dx, align[j] + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
        }
      }
    }
    this.drawFormatBits("l", 0); // 場所を確保する（値は最後に書き直す）
    if (this.version >= 7) {
      let rem = this.version;
      for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
      const bits = (this.version << 12) | rem;
      for (let i = 0; i < 18; i++) {
        const dark = ((bits >>> i) & 1) === 1;
        const a = n - 11 + (i % 3);
        const b = Math.floor(i / 3);
        this.set(a, b, dark);
        this.set(b, a, dark);
      }
    }
  }

  drawFormatBits(ecc: Ecc, mask: number): void {
    const data = (FORMAT_BITS[ecc] << 3) | mask;
    let rem = data;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const bits = ((data << 10) | rem) ^ 0x5412;
    const bit = (i: number) => ((bits >>> i) & 1) === 1;
    const n = this.size;
    for (let i = 0; i <= 5; i++) this.set(8, i, bit(i));
    this.set(8, 7, bit(6));
    this.set(8, 8, bit(7));
    this.set(7, 8, bit(8));
    for (let i = 9; i < 15; i++) this.set(14 - i, 8, bit(i));
    for (let i = 0; i < 8; i++) this.set(n - 1 - i, 8, bit(i));
    for (let i = 8; i < 15; i++) this.set(8, n - 15 + i, bit(i));
    this.set(8, n - 8, true); // 常に黒のモジュール
  }

  drawCodewords(data: Uint8Array): void {
    const pos = this.dataPos;
    const m = this.modules;
    const n = Math.min(pos.length, data.length * 8);
    for (let i = 0; i < n; i++) m[pos[i]] = (data[i >>> 3] >>> (7 - (i & 7))) & 1;
  }

  applyMask(mask: number): void {
    const pattern = maskPatterns(this)[mask];
    const m = this.modules;
    for (let k = 0; k < m.length; k++) m[k] ^= pattern[k];
  }

  /** マスクの評価（読み取りにくい模様の多さ）。小さいほど良い */
  penalty(): number {
    const n = this.size;
    const m = this.modules;
    let score = 0;
    let dark = 0;
    // 同じ色の連続（行・列）と、ファインダーに似た模様（1:1:3:1:1 の前後に白 4 つ）
    for (let a = 0; a < n; a++) score += linePenalty(m, a * n, 1, n) + linePenalty(m, a, n, n);
    // 2×2 の同じ色のかたまり
    for (let y = 0; y < n - 1; y++) {
      for (let x = 0, k = y * n; x < n - 1; x++, k++) {
        const v = m[k];
        dark += v;
        if (v === m[k + 1] && v === m[k + n] && v === m[k + n + 1]) score += 3;
      }
      dark += m[y * n + n - 1];
    }
    for (let k = (n - 1) * n; k < n * n; k++) dark += m[k];
    // 黒の割合が 50% から離れているほど悪い
    const total = n * n;
    score += (Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1) * 10;
    return score;
  }
}

/**
 * data を version・ecc の QR にする（バイトモード）。誤り訂正レベルは上げない。
 *
 * mask を指定すると、そのマスクを使う（8 種類を試して評価する手間を省く。どのマスクでも読み取りには支障がない）。
 * 省略すると、規格どおり読みにくい模様の少ないマスクを選ぶ（評価に生成時間の約 8 割がかかる）。
 */
export function encodeQr(data: Uint8Array, version: number, ecc: Ecc, mask?: number): QrMatrix {
  const codewords = interleave(dataCodewords(data, version, ecc), version, ecc);
  const b = new Builder(version);
  b.drawCodewords(codewords);
  if (mask !== undefined) {
    if (!(Number.isInteger(mask) && mask >= 0 && mask < 8)) throw new RangeError(`invalid mask: ${mask}`);
    b.applyMask(mask);
    b.drawFormatBits(ecc, mask);
    return { version, size: b.size, modules: b.modules };
  }
  let best = 0;
  let bestScore = Infinity;
  for (let mask = 0; mask < 8; mask++) {
    b.applyMask(mask);
    b.drawFormatBits(ecc, mask);
    const s = b.penalty();
    if (s < bestScore) {
      best = mask;
      bestScore = s;
    }
    b.applyMask(mask); // 元に戻す（XOR なので同じマスクをもう一度かける）
  }
  b.applyMask(best);
  b.drawFormatBits(ecc, best);
  return { version, size: b.size, modules: b.modules };
}
