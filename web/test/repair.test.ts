/**
 * 修復用フレーム（REPAIR）: Python 版と同じチャンクを重ねていること、取りこぼしがあっても再送なしに受信できること。
 */
import { describe, expect, it } from "vitest";
import { Assembler, type CompletionResult } from "../src/lib/assembler";
import { sha256Hex } from "../src/lib/codec";
import { parseFrame } from "../src/lib/protocol";
import { RepairDecoder, degree, repairIndices } from "../src/lib/repair";
import { type FixtureCase, b64, fixturesReady, readJson } from "./helpers";

const ready = fixturesReady();
const fixtures = ready
  ? readJson<{ cases: FixtureCase[]; repairVectors: { sessionId: number; index: number; total: number; indices: number[] }[] }>("frames.json")
  : { cases: [], repairVectors: [] };
const byId = (id: string) => fixtures.cases.find((c) => c.id === id)!;

function feedUntilDone(asm: Assembler, frames: Uint8Array[]): Promise<{ result: CompletionResult | null; used: number }> {
  return new Promise((resolve) => {
    let used = 0;
    let done = false;
    asm.onComplete = (result) => {
      done = true;
      resolve({ result, used });
    };
    for (const f of frames) {
      used++;
      asm.feed(parseFrame(f)!);
      const s = asm.snapshot();
      if (s.total && s.received === s.total) break; // 完了の通知は非同期なので、受信数で止める
    }
    setTimeout(() => !done && resolve({ result: null, used }), 2000);
  });
}

describe.skipIf(!ready)("Python 版との一致", () => {
  it("重ねるチャンクの番号が同じ", () => {
    expect(fixtures.repairVectors.length).toBeGreaterThan(0);
    for (const v of fixtures.repairVectors) {
      expect(repairIndices(v.sessionId, v.index, v.total), JSON.stringify([v.sessionId, v.index, v.total])).toEqual(v.indices);
    }
  });

  for (const id of ["repair_file", "repair_bundle"]) {
    it(`${id}: DATA を 3 分の 1 取りこぼしても、修復用フレームで完了する`, async () => {
      const c = byId(id);
      const data = c.data.map(b64).filter((_, i) => i % 3 !== 1);
      const frames = [b64(c.meta), ...data, ...c.repair.map(b64)];
      const { result, used } = await feedUntilDone(new Assembler(), frames);
      expect(result?.ok, result?.message).toBe(true);
      expect(used).toBeLessThan(frames.length); // 修復用フレームを使い切る前に完了
      if (c.mode === "file") expect(await sha256Hex(result!.file!.data)).toBe(c.rawSha256);
    });
  }

  it("META より前の修復用フレームは使わない（META のあとで届けば使う）", async () => {
    const c = byId("repair_file");
    const asm = new Assembler();
    expect(asm.feed(parseFrame(b64(c.repair[0]))!)).toBe("dup");
    const data = c.data.map(b64).filter((_, i) => i !== 3);
    const { result } = await feedUntilDone(asm, [b64(c.meta), ...data, ...c.repair.map(b64)]);
    expect(result?.ok).toBe(true);
  });

  it("長さが違う修復用フレームは不正として扱う", () => {
    const c = byId("repair_file");
    const asm = new Assembler();
    asm.feed(parseFrame(b64(c.meta))!);
    const bad = parseFrame(b64(c.repair[0]))!;
    expect(asm.feed({ ...bad, payload: bad.payload.subarray(1) })).toBe("invalid");
  });
});

describe("RepairDecoder", () => {
  function roundtrip(total: number, len: number, loss: number, seed: number): number {
    let s = seed;
    const rand = () => ((s = (s * 1103515245 + 12345) & 0x7fffffff) / 0x80000000);
    const chunks = Array.from({ length: total }, () => Uint8Array.from({ length: len }, () => (rand() * 256) | 0));
    const got = new Map<number, Uint8Array>();
    const dec = new RepairDecoder(total, len);
    let used = 0;
    const take = (solved: [number, Uint8Array][]) => solved.forEach(([q, d]) => got.set(q, d));
    for (let i = 0; i < total && got.size < total; i++) {
      if (rand() < loss) continue;
      used++;
      got.set(i, chunks[i]);
      take(dec.addKnown(i, chunks[i]));
    }
    for (let r = 0; got.size < total && r < total * 3; r++) {
      if (rand() < loss) continue;
      used++;
      const idx = repairIndices(7, r, total);
      const p = new Uint8Array(len);
      for (const i of idx) for (let k = 0; k < len; k++) p[k] ^= chunks[i][k];
      take(dec.addRepair(idx, p, (i) => got.get(i)));
    }
    expect(got.size).toBe(total);
    for (let i = 0; i < total; i++) expect(got.get(i)).toEqual(chunks[i]);
    return used;
  }

  it("取りこぼしの率によらず、全チャンク数をわずかに超える枚数で解ける", () => {
    for (const [total, loss] of [[1, 0.5], [7, 0.4], [300, 0.05], [300, 0.5], [3000, 0.2]]) {
      const used = roundtrip(total, 64, loss, total + Math.round(loss * 100));
      expect(used, `${total} ${loss}`).toBeLessThanOrEqual(total + 10);
    }
  });

  it("保持する式の数に上限がある（偽造した修復用フレームでメモリを使い切らせない）", () => {
    const dec = new RepairDecoder(1000, 200, 10 * (Math.ceil(1000 / 32) * 4 + 200 + 64));
    expect(dec.maxRows).toBe(10);
    for (let r = 0; r < 30; r++) dec.addRepair(repairIndices(9, r, 1000), new Uint8Array(200), () => undefined);
    expect(dec.pending).toBe(10);
    expect(dec.dropped).toBe(20);
  });

  it("重ねる数", () => {
    expect([degree(1), degree(2), degree(10), degree(2049), degree(500000)]).toEqual([1, 1, 5, 1024, 1024]);
  });
});
