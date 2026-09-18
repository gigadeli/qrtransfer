/**
 * 取りこぼしの割合の推定。
 *
 * 送信側は QR を決まった順（DATA 0, 1, 2, … → 修復用 0, 1, 2, …）に表示するので、直近に読めた QR の
 * 位置（何番目の QR か）の範囲と、そのうち実際に読めた数を比べれば、読めずに流れていった割合がわかる。
 * 送信側の表示速度や「同時に表示する QR」の数が、受信側の読み取り速度に対して多すぎないかの目安になる。
 */

export const LOSS_WINDOW_MS = 5000;
const MIN_SPAN = 20; // 位置の範囲がこれより狭いうちは推定しない（少なすぎて当てにならない）
const WRAP = 32; // 位置がこれ以上戻ったら、送信側が 1 周した（または再送モードに入った）とみなす

export class LossMeter {
  private seen: { t: number; pos: number }[] = [];

  /** 読めた QR の位置を記録する（同じ QR を何度読んでもよい）。 */
  add(pos: number, now = performance.now()): void {
    this.seen.push({ t: now, pos });
    while (this.seen.length && now - this.seen[0].t > LOSS_WINDOW_MS) this.seen.shift();
  }

  /** 直近の取りこぼしの割合（0〜1）。推定できないときは null。 */
  rate(now = performance.now()): number | null {
    const recent = this.seen.filter((e) => now - e.t <= LOSS_WINDOW_MS);
    // 最後に位置が大きく戻ったところより後だけを使う（1 周して先頭に戻った前後をまたがないように）
    let start = 0;
    let max = -Infinity;
    for (let i = 0; i < recent.length; i++) {
      if (recent[i].pos < max - WRAP) {
        start = i;
        max = -Infinity;
      }
      max = Math.max(max, recent[i].pos);
    }
    const positions = new Set(recent.slice(start).map((e) => e.pos));
    if (!positions.size) return null;
    const span = Math.max(...positions) - Math.min(...positions) + 1;
    if (span < MIN_SPAN) return null;
    return Math.max(0, 1 - positions.size / span);
  }
}
