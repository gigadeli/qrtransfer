/**
 * 送信側（Web 版）で作ったデータの確認。
 * - Python 版と同じ入力からは、同じバイト列のフレームができる
 * - Web 版の受信（Assembler）で受け取れる（修復用 QR・再送を含む）
 */
import { describe, expect, it } from "vitest";
import { readTar } from "../src/lib/archive";
import { Assembler, type CompletionResult } from "../src/lib/assembler";
import { CAROUSEL_META, carouselOrder, chooseVersion, framesPerCycle, gridLayout, repairIndex } from "../src/lib/carousel";
import { sha256Hex } from "../src/lib/codec";
import { metaPayload, parseMeta, truncateName } from "../src/lib/meta";
import { MAX_RAW_SIZE, Plan, type SendItem, PackError, makePlan, packItems } from "../src/lib/pack";
import { parseFrame, parseRanges } from "../src/lib/protocol";
import { repairCount } from "../src/lib/repair";
import { type FixtureCase, b64, fixturesReady, readJson } from "./helpers";

const ready = fixturesReady();
const cases = ready ? readJson<{ cases: FixtureCase[] }>("frames.json").cases : [];

const text = (s: string) => new Blob([new TextEncoder().encode(s)]);
const file = (name: string, data: Blob | string, mtime = 1_700_000_000): SendItem => ({
  kind: "file",
  name,
  data: typeof data === "string" ? text(data) : data,
  mtime,
});
const dir = (name: string, children: SendItem[]): SendItem => ({ kind: "dir", name, children, mtime: 1_700_000_000 });

function randomBlob(n: number, seed = 1): Blob {
  const out = new Uint8Array(n);
  let s = seed;
  for (let i = 0; i < n; i++) {
    s = (Math.imul(s, 1103515245) + 12345) >>> 0;
    out[i] = s >>> 24;
  }
  return new Blob([out]);
}

/** 表示順のとおりにフレームを作る */
function framesOf(plan: Plan, order: Int32Array, maxFrame?: number): Uint8Array[] {
  return [...order].map((e) => (e === CAROUSEL_META ? plan.metaFrame(maxFrame) : repairIndex(e) !== null ? plan.repairFrame(repairIndex(e)!) : plan.dataFrame(e)));
}

function receive(frames: Uint8Array[], asm = new Assembler()): Promise<CompletionResult> {
  return new Promise((resolve, reject) => {
    asm.onComplete = resolve;
    for (const f of frames) {
      const parsed = parseFrame(f);
      if (!parsed) return reject(new Error("フレームを解析できません"));
      asm.feed(parsed);
    }
    setTimeout(() => reject(new Error("完了しませんでした")), 5000);
  });
}

describe.skipIf(!ready)("Python 版と同じフレームを作る", () => {
  for (const id of ["random_none", "repair_file", "empty"]) {
    it(id, () => {
      const c = cases.find((x) => x.id === id)!;
      const meta = parseMeta(parseFrame(b64(c.meta))!.payload)!;
      const payload = new Uint8Array(c.data.flatMap((f) => [...parseFrame(b64(f))!.payload]));
      // Python 版の Plan と同じ中身から作る
      const plan = new Plan(c.sessionId, meta, payload, meta.chunk_size);
      expect(plan.metaFrame()).toEqual(b64(c.meta));
      c.data.forEach((f, i) => expect(plan.dataFrame(i)).toEqual(b64(f)));
      c.repair.forEach((f, r) => expect(plan.repairFrame(r)).toEqual(b64(f)));
    });
  }
});

describe("Web 版の受信で受け取れる", () => {
  it("1 つのファイル（zlib で圧縮される）", async () => {
    const body = "QRTransfer の送信テスト。".repeat(400);
    const packed = await packItems([file("メモ.txt", body)]);
    expect(packed.mode).toBe("file");
    const plan = await makePlan(packed, 300, "test");
    expect(plan.meta.compression).toBe("zlib");
    expect(plan.meta.mtime).toBe(1_700_000_000);
    const r = await receive(framesOf(plan, carouselOrder(plan.total)));
    expect(r.ok).toBe(true);
    expect(r.file!.fileName).toBe("メモ.txt");
    expect(new TextDecoder().decode(r.file!.data)).toBe(body);
  });

  it("圧縮が効かないファイルは圧縮しない", async () => {
    const plan = await makePlan(await packItems([file("random.bin", randomBlob(5000))]), 800, "test");
    expect(plan.meta.compression).toBe("none");
    expect(plan.meta.sha256_payload).toBe(plan.meta.sha256_raw);
    expect((await receive(framesOf(plan, carouselOrder(plan.total)))).ok).toBe(true);
  });

  it("空のファイル", async () => {
    const plan = await makePlan(await packItems([file("empty.txt", "")]), 800, "test");
    expect(plan.total).toBe(0);
    const r = await receive(framesOf(plan, carouselOrder(plan.total)));
    expect(r.ok).toBe(true);
    expect(r.file!.data.length).toBe(0);
  });

  it("フォルダ（日本語の名前・空のフォルダ・長い名前を含む）", async () => {
    const long = `${"とても長い名前".repeat(12)}.txt`;
    const root = dir("写真", [
      file("a.txt", "aaa"),
      dir("中身", [file(long, "long"), dir("空", [])]),
      file("b.bin", randomBlob(3000, 7)),
    ]);
    const packed = await packItems([root]);
    expect(packed.mode).toBe("bundle");
    expect(packed.name).toBe("写真");
    expect(packed.fileCount).toBe(3);
    // Python 版と同じく、フォルダ自身は含めず中身から
    const tar = readTar(packed.raw);
    expect(tar.entries.map((e) => e.path)).toEqual(["a.txt", "b.bin", "中身", `中身/${long}`, "中身/空"]);
    expect(tar.entries.find((e) => e.path === "中身/空")!.dir).toBe(true);
    expect(new TextDecoder().decode(tar.entries.find((e) => e.path === `中身/${long}`)!.data)).toBe("long");
    expect(tar.entries[0].mtime).toBe(1_700_000_000);

    const plan = await makePlan(packed, 500, "test");
    const r = await receive(framesOf(plan, carouselOrder(plan.total)));
    expect(r.ok).toBe(true);
    expect(r.file!.fileName).toBe("写真.zip");
    expect(r.file!.fileCount).toBe(3);
  });

  it("複数のファイル（名前が重なれば連番を付ける）", async () => {
    const packed = await packItems([file("a.txt", "1"), file("A.txt", "2"), dir("d", [file("x", "3")])], new Date(2026, 8, 19, 9, 5, 7));
    expect(packed.name).toBe("bundle_20260919_090507");
    expect(readTar(packed.raw).entries.map((e) => e.path)).toEqual(["a.txt", "A (1).txt", "d", "d/x"]);
  });

  it("修復用 QR: DATA を 3 割取りこぼしても、修復用 QR で復元できる", async () => {
    const plan = await makePlan(await packItems([file("r.bin", randomBlob(20_000, 3))]), 200, "test");
    const repairs = repairCount(plan.total, 100);
    const order = carouselOrder(plan.total, null, repairs);
    expect(order.length).toBe(framesPerCycle(plan.total, repairs));
    const frames = framesOf(plan, order).filter((_, i) => order[i] < 0 || order[i] % 10 >= 3);
    const r = await receive(frames);
    expect(r.ok).toBe(true);
    expect(await sha256Hex(r.file!.data)).toBe(plan.meta.sha256_raw);
  });

  it("再送: 欠けた番号だけを送れば完了する", async () => {
    const plan = await makePlan(await packItems([file("r.bin", randomBlob(8000, 5))]), 400, "test");
    const asm = new Assembler();
    const first = framesOf(plan, carouselOrder(plan.total)).filter((f) => {
      const p = parseFrame(f)!;
      return !(p.type === 1 && [2, 3, 4, 11].includes(p.seq));
    });
    for (const f of first) asm.feed(parseFrame(f)!);
    expect(asm.missingText()).toBe("2-4,11");
    const seqs = parseRanges(asm.missingText(), plan.total);
    const r = await receive(framesOf(plan, carouselOrder(plan.total, seqs)), asm);
    expect(r.ok).toBe(true);
  });

  it("100 MB を超えるものは送らない", async () => {
    const big = { size: 101 * 1024 * 1024 } as Blob;
    await expect(packItems([file("big.bin", big)])).rejects.toThrow(PackError);
  });

  it("ファイルの合計が 100 MB ちょうどでも、まとめた tar が超えるなら送らない（受信側が拒否するため）", async () => {
    const half = new Blob([new Uint8Array(MAX_RAW_SIZE / 2)]);
    await expect(packItems([file("a.bin", half), file("b.bin", half)])).rejects.toThrow(/まとめると/);
  });
});

describe("表示順・バージョン・並べ方（Python 版と同じ決まり）", () => {
  it("表示順: 先頭は META、20 枚ごとに META、修復用は DATA の後ろ", () => {
    const o = [...carouselOrder(45, null, 3)];
    expect(o.length).toBe(framesPerCycle(45, 3));
    expect(o.slice(0, 3)).toEqual([-1, 0, 1]);
    expect(o[21]).toBe(-1);
    expect(o.slice(-4)).toEqual([44, -2, -3, -4]);
    expect([...carouselOrder(10, [7, 3, 3, 99])]).toEqual([-1, 3, 7]);
    expect([...carouselOrder(0)]).toEqual([-1]);
  });

  it("バージョンは DATA と、名前を切り詰めた META の両方が収まる最小のもの", () => {
    expect(chooseVersion(820, 300, "l")).toBe(20); // v20-L は 858 バイト
    expect(chooseVersion(820, 300, "m")).toBe(23);
    expect(() => chooseVersion(2020, 300, "h")).toThrow();
  });

  it("並べ方: いちばん大きく表示できる並べ方を選び、1 マスが整数ピクセルになる大きさにそろえる", () => {
    expect(gridLayout(4, 1920, 1080, 105)).toEqual([2, 2, 525]); // 横一列だと 1 個 480
    expect(gridLayout(4, 1920, 400, 105)).toEqual([4, 1, 400]); // 105 の整数倍（315）は 93% に届かないので、そのまま
    expect(gridLayout(2, 1000, 1000, 105)).toEqual([1, 2, 500]);
    expect(gridLayout(1, 1920, 1080, 105)).toEqual([1, 1, 1050]);
  });

  it("名前の切り詰めは拡張子を残す", () => {
    expect(truncateName("あいうえおかきくけこ.txt", 20)).toBe("あいうえお~.txt"); // 15 + 1 + 4 = 20
    const meta = { ...parseMeta(new TextEncoder().encode(JSON.stringify({ name: "x", mode: "file", compression: "none", raw_size: 0, payload_size: 0, chunk_size: 800, total: 0, sha256_raw: "0".repeat(64), sha256_payload: "0".repeat(64) })))!, name: "長い名前".repeat(50) };
    expect(metaPayload(meta, 300).length).toBeLessThanOrEqual(300);
  });

  it("欠落番号の入力（全角・改行も受け付ける）", () => {
    expect([...parseRanges("１，３－５\n７", 10)]).toEqual([1, 3, 4, 5, 7]);
    expect(parseRanges("  ").size).toBe(0);
    expect(() => parseRanges("3-1")).toThrow();
    expect(() => parseRanges("10", 10)).toThrow();
    expect(() => parseRanges("1,,2")).toThrow();
  });
});
