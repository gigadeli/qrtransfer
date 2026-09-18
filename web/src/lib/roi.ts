/**
 * 読み取る範囲（ROI）の追跡。QR の位置がわかっている間は、その周辺だけを切り出して読む（全体を読むより軽い）。
 *
 * 送信側が QR を複数並べているときは、読めた QR 全体を囲む範囲にする。一部しか読めなかった画像で範囲を
 * 狭めると、残りの QR が範囲の外になって読めなくなるので、前より少ない数しか読めなかったときは範囲を変えない。
 */
import { roiOf } from "./qr";

export type Roi = [number, number, number, number];

export interface Tracked {
  roi: Roi;
  w: number;
  h: number;
  misses: number; // 1 つも読めなかった回数（続けて）
  expected: number; // 範囲を決めたときに読めた QR の数
  short: number; // expected より少なくしか読めなかった回数（続けて）
}

export const ROI_MISSES = 10; // 周辺だけを読んで、読めない（または一部しか読めない）回数がこれだけ続いたら全体から探し直す

/** 1 回の読み取り結果 found（正しく読めた QR の位置）から、次に読む範囲を決める。null は映像全体。 */
export function nextRoi(tracked: Tracked | null, found: [number, number][][], w: number, h: number): Tracked | null {
  const same = tracked && tracked.w === w && tracked.h === h ? tracked : null;
  if (!found.length) {
    if (!same) return null;
    return ++same.misses >= ROI_MISSES ? null : same;
  }
  if (same && found.length < same.expected) {
    // 一部しか読めなかった（切り替わりの途中など）。範囲は保ったまま、続くようなら全体から探し直す
    same.misses = 0;
    return ++same.short >= ROI_MISSES ? null : same;
  }
  return { roi: roiOf(found.flat(), w, h), w, h, misses: 0, expected: found.length, short: 0 };
}
