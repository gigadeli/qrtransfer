/**
 * 表示の順番（1 周分）と、QR のバージョン・並べ方の決定（Python 版 packer.carousel_order、qrgen.choose_version、
 * fullscreen_qr.grid_layout と同じ決まり）。
 */
import { type Ecc, MAX_VERSION, capacity, symbolSize } from "./qrcode";
import { META_INTERVAL } from "./protocol";

/** 表示順の中で META を表す値 */
export const CAROUSEL_META = -1;
const CAROUSEL_REPAIR = -2;

/** 表示順の中で r 番目の修復用フレームを表す値 */
export function carouselRepair(r: number): number {
  return CAROUSEL_REPAIR - r;
}

/** 表示順の値が修復用フレームなら、その番号。そうでなければ null */
export function repairIndex(entry: number): number | null {
  return entry <= CAROUSEL_REPAIR ? CAROUSEL_REPAIR - entry : null;
}

/**
 * 1 周分の表示順。先頭は必ず META、DATA（と修復用）metaInterval 枚ごとに META を挟む。
 * seqs を指定すると、その DATA だけを送る（再送。修復用フレームは付けない）。
 */
export function carouselOrder(total: number, seqs: Iterable<number> | null = null, repairs = 0, metaInterval = META_INTERVAL): Int32Array {
  let data: number[];
  if (seqs === null) {
    data = Array.from({ length: total }, (_, i) => i);
    for (let r = 0; r < repairs; r++) data.push(carouselRepair(r));
  } else {
    data = [...new Set([...seqs].filter((s) => s >= 0 && s < total))].sort((a, b) => a - b);
  }
  const n = data.length + Math.max(1, Math.ceil(data.length / metaInterval));
  const order = new Int32Array(n);
  let o = 0;
  for (let i = 0; i < data.length; i++) {
    if (i % metaInterval === 0) order[o++] = CAROUSEL_META;
    order[o++] = data[i];
  }
  if (!data.length) order[o++] = CAROUSEL_META;
  return order;
}

export function framesPerCycle(total: number, repairs = 0): number {
  return total + repairs + Math.max(1, Math.ceil((total + repairs) / META_INTERVAL));
}

/** 余白（クワイエットゾーン）のモジュール数 */
export const QUIET_ZONE = 4;

export function modulesWithBorder(version: number): number {
  return symbolSize(version) + QUIET_ZONE * 2;
}

export class VersionError extends Error {}

/** 全フレーム共通の QR バージョン（DATA の最大フレームと、名前を切り詰めた META が収まる最小のもの） */
export function chooseVersion(maxDataFrame: number, minMetaFrame: number, ecc: Ecc): number {
  const need = Math.max(maxDataFrame, minMetaFrame);
  for (let v = 1; v <= MAX_VERSION; v++) if (capacity(v, ecc) >= need) return v;
  throw new VersionError(
    `1 枚 ${need} バイトは、誤り訂正 ${ecc.toUpperCase()} の QR（最大 ${capacity(MAX_VERSION, ecc)} バイト）に収まりません。` +
      "チャンクサイズを小さくするか、誤り訂正を下げてください。",
  );
}

/**
 * count 個の正方形の QR を areaW × areaH に並べるときの [列数, 行数, 1 個の一辺]。一辺が最も大きくなる並べ方を選ぶ。
 * 1 マスが整数ピクセルになる大きさで十分近ければ、それに合わせる（マスの幅がそろい、読み取りやすい）。
 */
export function gridLayout(count: number, areaW: number, areaH: number, modules: number): [number, number, number] {
  let best: [number, number, number] = [1, count, 0];
  for (let cols = 1; cols <= count; cols++) {
    const rows = Math.ceil(count / cols);
    const avail = Math.max(1, Math.min(Math.floor(areaW / cols), Math.floor(areaH / rows)));
    if (avail > best[2]) best = [cols, rows, avail];
  }
  const [cols, rows, avail] = best;
  const scale = Math.floor(avail / modules);
  const side = scale >= 1 && modules * scale >= avail * 0.93 ? modules * scale : avail;
  return [cols, rows, side];
}
