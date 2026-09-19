import { describe, expect, it } from "vitest";
import { FlipScheduler } from "../src/send/scheduler";

const FRAME = 1000 / 60; // 60 Hz の画面の書き換え間隔

/** 60 Hz の書き換えごとに due を呼び、seconds 秒分の切り替えを記録する */
function run(s: FlipScheduler, fps: number, seconds: number, ready = (_: number) => true) {
  const flips: { t: number; cell: number; pos: number }[] = [];
  s.begin(0, fps);
  for (let t = 0; t <= seconds * 1000; t += FRAME) {
    const before = [...s.cells];
    const { flipped } = s.due(t, fps, ready);
    for (const i of flipped) flips.push({ t, cell: i, pos: s.cells[i] });
    // 同じ書き換えの中で切り替わるのは、時刻の来たものだけ（並べる数以下）
    expect(flipped.length).toBeLessThanOrEqual(s.cells.length);
    expect(new Set(s.cells).size).toBe(s.cells.length); // 同じ QR が 2 つ並ばない
    if (flipped.length === 1) expect(s.cells.filter((p, i) => p !== before[i]).length).toBe(1);
  }
  return flips;
}

describe("並べた QR の切り替え（時間差）", () => {
  it("4 個・10 fps: 1 秒に 40 回、1 つずつ切り替わり、表示順を飛ばさない", () => {
    const s = new FlipScheduler(200, 4);
    const flips = run(s, 10, 2);
    expect(flips.length).toBeGreaterThanOrEqual(79);
    expect(flips.length).toBeLessThanOrEqual(81);
    // 位置 0,1,2,3 の順に 1 つずつ
    expect(flips.slice(0, 8).map((f) => f.cell)).toEqual([0, 1, 2, 3, 0, 1, 2, 3]);
    // 表示順を 1 つずつ進める（4 個並べた後の 4 から）
    expect(flips.map((f) => f.pos)).toEqual(flips.map((_, k) => 4 + k));
    // 同じ位置の QR は約 100 ms（1/fps）ごとに替わる
    const cell0 = flips.filter((f) => f.cell === 0).map((f) => f.t);
    for (let k = 1; k < cell0.length; k++) expect(cell0[k] - cell0[k - 1]).toBeCloseTo(100, -1.5);
  });

  it("1 個なら従来どおり 1/fps ごとに替わる", () => {
    const s = new FlipScheduler(50, 1);
    const flips = run(s, 6, 1.05);
    expect(flips.length).toBe(6);
    expect(flips.every((f) => f.cell === 0)).toBe(true);
  });

  it("1 周したら周回が増え、先頭に戻る", () => {
    const s = new FlipScheduler(10, 2);
    run(s, 30, 1);
    expect(s.cycle).toBeGreaterThanOrEqual(3);
    expect(s.cells.every((p) => p >= 0 && p < 10)).toBe(true);
  });

  it("QR の用意が間に合わないときは、飛ばさずに待つ", () => {
    const s = new FlipScheduler(100, 2);
    s.begin(0, 10);
    expect(s.due(100, 10, (p) => p < 3)).toEqual({ flipped: [0], missing: true });
    expect(s.cells).toEqual([2, 1]);
    expect(s.due(200, 10, () => false).missing).toBe(true);
    expect(s.nextPos).toBe(3);
  });

  it("QR の用意を待った後は、同時に切り替えず 1 つずつ切り替え直す", () => {
    const s = new FlipScheduler(100, 4);
    let ready = 5;
    const flips: { t: number; n: number }[] = [];
    s.begin(0, 10);
    for (let t = 0; t < 400; t += FRAME) {
      if (t > 90) ready = 100; // 90 ms の間、QR の用意が遅れた
      const { flipped } = s.due(t, 10, (p) => p < ready);
      if (flipped.length) flips.push({ t, n: flipped.length });
    }
    expect(flips.every((f) => f.n === 1)).toBe(true);
    // 再開後は 1/(fps × 並べる数) = 25 ms ずつ
    const after = flips.filter((f) => f.t > 90).map((f) => f.t);
    for (let k = 1; k < after.length; k++) expect(after[k] - after[k - 1]).toBeGreaterThanOrEqual(FRAME - 0.01);
  });

  it("長く止まっていた後は、まとめて切り替えない", () => {
    const s = new FlipScheduler(100, 4);
    s.begin(0, 10);
    const { flipped } = s.due(5000, 10, () => true);
    expect(flipped.length).toBe(1);
  });

  it("並べ直すと、続きから並ぶ", () => {
    const s = new FlipScheduler(10, 4);
    s.reset(8, 4);
    expect(s.cells).toEqual([8, 9, 0, 1]);
    expect(s.nextPos).toBe(2);
    s.reset(0, 20); // 1 周より多くは並べない
    expect(s.cells.length).toBe(10);
  });
});
