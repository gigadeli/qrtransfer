import { describe, expect, it } from "vitest";
import { Assembler } from "../src/lib/assembler";
import { LOSS_WINDOW_MS, LossMeter } from "../src/lib/loss";
import { parseFrame } from "../src/lib/protocol";
import { type FixtureCase, b64, fixturesReady, readJson } from "./helpers";

describe("取りこぼしの推定", () => {
  it("順に表示される QR のうち、読めなかった割合を求める", () => {
    const m = new LossMeter();
    for (let p = 0; p < 100; p++) if (p % 4 !== 0) m.add(p, 1000 + p); // 4 つに 1 つ取りこぼす
    expect(m.rate(1100)).toBeCloseTo(0.25, 1);
  });

  it("同じ QR を何度読んでも数は増えない。少なすぎるうちは推定しない", () => {
    const m = new LossMeter();
    for (let k = 0; k < 5; k++) for (let p = 0; p < 10; p++) m.add(p, 1000 + k);
    expect(m.rate(1010)).toBeNull();
    for (let p = 10; p < 40; p++) m.add(p, 1010);
    expect(m.rate(1010)).toBe(0);
  });

  it("送信側が 1 周して先頭に戻ったら、戻った後だけで数える", () => {
    const m = new LossMeter();
    for (let p = 900; p < 1000; p += 2) m.add(p, 1000); // 半分取りこぼし
    for (let p = 0; p < 40; p++) m.add(p, 1001); // 1 周して先頭から。取りこぼしなし
    expect(m.rate(1001)).toBe(0);
  });

  it("古い記録は使わない", () => {
    const m = new LossMeter();
    for (let p = 0; p < 100; p += 2) m.add(p, 0);
    expect(m.rate(LOSS_WINDOW_MS + 1)).toBeNull();
  });
});

describe.skipIf(!fixturesReady())("受信中の取りこぼし（Assembler）", () => {
  it("DATA と修復用 QR の並びから取りこぼしを推定する", () => {
    const c = readJson<{ cases: FixtureCase[] }>("frames.json").cases.find((x) => x.id === "repair_file")!;
    const asm = new Assembler();
    asm.feed(parseFrame(b64(c.meta))!);
    c.data.forEach((f, i) => {
      if (i % 3 !== 0) asm.feed(parseFrame(b64(f))!); // DATA の 3 つに 1 つを取りこぼす
    });
    const loss = asm.snapshot().loss;
    expect(loss).not.toBeNull();
    expect(loss!).toBeGreaterThan(0.25);
    expect(loss!).toBeLessThan(0.4);
    // 修復用 QR はその続き（DATA の後ろ）の位置として数える。取りこぼしなく読めば割合は下がっていく
    for (const f of c.repair.slice(0, 5)) asm.feed(parseFrame(b64(f))!);
    expect(asm.snapshot().loss!).toBeLessThan(loss!);
  });
});
