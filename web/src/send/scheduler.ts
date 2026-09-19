/**
 * 並べた QR の切り替えの予定（送信画面の表示から、時間の計算だけを取り出したもの）。
 *
 * 並べた QR は 1 つずつ時間をずらして切り替える。各 QR は 1/fps ずつ表示し、並べた QR の間では
 * 1/(fps × 並べる数) ずつずらす。1 秒あたりに出す QR の数（fps × 並べる数）は、一斉に切り替える場合と同じ。
 */
export class FlipScheduler {
  cells: number[] = []; // 並べた位置ごとに表示している、表示順の中の位置
  cursor = 0; // 次に切り替える位置
  nextPos = 0; // 次に出す表示順の位置
  base = 0; // 一斉に並べ直したときの先頭（コマ送りの基準）
  cycle = 1; // 何周目か
  nextAt = 0; // 次に切り替える時刻（ミリ秒）
  private waiting = false; // QR の用意を待っている

  constructor(
    public length: number, // 1 周の枚数
    count: number,
    start = 0,
  ) {
    this.reset(start, count);
  }

  private at(p: number): number {
    return ((p % this.length) + this.length) % this.length;
  }

  /** start から続けて並べ直す（一斉に切り替える。開始・コマ送り・並べる数の変更のとき） */
  reset(start: number, count: number): void {
    const n = Math.max(1, Math.min(count, this.length));
    this.base = this.at(start);
    this.cells = Array.from({ length: n }, (_, i) => this.at(this.base + i));
    this.cursor = 0;
    this.nextPos = this.at(this.base + n);
  }

  /** 送信を始める（再開する）。最初の切り替えは 1 つ分の間隔の後 */
  begin(now: number, fps: number): void {
    this.nextAt = now + 1000 / fps / this.cells.length;
    this.waiting = false;
  }

  /**
   * now までに予定の来た切り替えを行い、切り替えた位置（並べた位置の番号）を返す。
   * ready(p) が false の QR（まだ作れていない）の所で止まる（飛ばさずに待つ）。そのとき missing = true。
   * 長く止まっていた後や、QR の用意を待った後は、まとめて切り替えない（遅れを取り戻そうとして、
   * 並べた QR が同時に切り替わらないようにする。待った後は、その時点から 1 つずつ切り替え直す）。
   */
  due(now: number, fps: number, ready: (p: number) => boolean): { flipped: number[]; missing: boolean } {
    const flipped: number[] = [];
    if (now < this.nextAt) return { flipped, missing: false };
    const n = this.cells.length;
    const interval = 1000 / fps;
    const sub = interval / n;
    if (now - this.nextAt > interval || this.waiting) this.nextAt = now;
    while (now >= this.nextAt) {
      const p = this.nextPos;
      this.waiting = !ready(p);
      if (this.waiting) return { flipped, missing: true };
      const i = this.cursor;
      this.cells[i] = p;
      this.nextPos = this.at(p + 1);
      if (this.nextPos === 0) this.cycle++;
      this.cursor = (i + 1) % n;
      if (this.cursor === 0) this.base = this.cells[0];
      this.nextAt += sub;
      flipped.push(i);
    }
    return { flipped, missing: false };
  }
}
