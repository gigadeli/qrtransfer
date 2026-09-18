/**
 * デスクトップ版（Python）の送信処理で作ったフレーム・QR 画像を、Web 版で受信できるかの確認。
 * テストデータは `npm run fixtures`（web/scripts/make_fixtures.py）で作る。
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { beforeAll, describe, expect, it } from "vitest";
import { readTar } from "../src/lib/archive";
import { Assembler, type CompletionResult } from "../src/lib/assembler";
import { sha256Hex } from "../src/lib/codec";
import { parseFrame } from "../src/lib/protocol";
import { RobustReader, crop, decodeImage } from "../src/lib/qr";
import { FIXTURES, type FixtureCase, b64, fixturesReady, readJson, useLocalWasm } from "./helpers";

const ready = fixturesReady();
const cases = ready ? readJson<{ cases: FixtureCase[] }>("frames.json").cases : [];
const byId = (id: string) => cases.find((c) => c.id === id)!;

function shuffled<T>(xs: T[], seed = 1): T[] {
  const a = [...xs];
  let s = seed;
  for (let i = a.length - 1; i > 0; i--) {
    s = (s * 1103515245 + 12345) & 0x7fffffff;
    const j = s % (i + 1);
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function receive(asm: Assembler, frames: Uint8Array[]): Promise<CompletionResult> {
  return new Promise((resolve) => {
    asm.onComplete = resolve;
    for (const f of frames) {
      const parsed = parseFrame(f);
      if (parsed) asm.feed(parsed);
    }
  });
}

async function unzipNames(zip: Uint8Array): Promise<Record<string, string>> {
  // makeZip は無圧縮なので、ローカルヘッダーを順にたどれば中身を取り出せる
  const out: Record<string, string> = {};
  const v = new DataView(zip.buffer, zip.byteOffset, zip.byteLength);
  let off = 0;
  while (v.getUint32(off, true) === 0x04034b50) {
    const size = v.getUint32(off + 18, true);
    const nameLen = v.getUint16(off + 26, true);
    const name = new TextDecoder().decode(zip.subarray(off + 30, off + 30 + nameLen));
    const data = zip.subarray(off + 30 + nameLen, off + 30 + nameLen + size);
    if (!name.endsWith("/")) out[name] = await sha256Hex(data);
    off += 30 + nameLen + size;
  }
  return out;
}

describe.skipIf(!ready)("Python 版で作ったフレームを受信できる", () => {
  for (const id of ["text_zlib", "text_lzma", "random_none", "auto", "empty"]) {
    it(`file: ${id}`, async () => {
      const c = byId(id);
      const frames = shuffled([...c.data, c.meta].map(b64));
      const r = await receive(new Assembler(), [...frames, ...frames]); // 重複も混ぜる
      expect(r.ok, r.message).toBe(true);
      expect(r.file!.fileName).toBe(c.name);
      expect(await sha256Hex(r.file!.data)).toBe(c.rawSha256);
    });
  }

  it("bundle: フォルダは ZIP にまとめ直す（長い名前・日本語・空フォルダ）", async () => {
    const c = byId("bundle");
    const r = await receive(new Assembler(), shuffled([c.meta, ...c.data].map(b64), 7));
    expect(r.ok, r.message).toBe(true);
    expect(r.file!.fileName).toBe("フォルダ.zip");
    expect(r.file!.fileCount).toBe(Object.keys(c.files!).length);
    expect(await unzipNames(r.file!.data)).toEqual(c.files);
  });

  it("bundle: 危険なパスはスキップする", async () => {
    const c = byId("evil");
    const r = await receive(new Assembler(), [c.meta, ...c.data].map(b64));
    expect(r.ok).toBe(true);
    expect(r.file!.skipped!.length).toBe(c.skipped);
    expect(await unzipNames(r.file!.data)).toEqual(c.files);
  });

  it("META が最後でも、欠落があっても揃えば完了する", async () => {
    const c = byId("random_none");
    const asm = new Assembler();
    const data = c.data.map(b64);
    for (const f of data.filter((_, i) => i % 3 !== 0)) asm.feed(parseFrame(f)!);
    const missing = asm.missing();
    expect(missing).toEqual(data.map((_, i) => i).filter((i) => i % 3 === 0));
    const r = await receive(asm, [...missing.map((i) => data[i]), b64(c.meta)]);
    expect(r.ok).toBe(true);
    expect(await sha256Hex(r.file!.data)).toBe(c.rawSha256);
  });

  it("別の転送を検出して切り替えられる", async () => {
    const a = byId("random_none");
    const b = byId("other_session");
    const asm = new Assembler();
    asm.feed(parseFrame(b64(a.data[0]))!);
    expect(asm.feed(parseFrame(b64(b.data[0]))!)).toBe("conflict");
    expect(asm.snapshot().conflictSessionId).toBe(b.sessionId);
    expect(asm.switchToConflict()).toBe(true);
    const r = await receive(asm, [b.meta, ...b.data].map(b64));
    expect(r.ok).toBe(true);
    expect(r.sessionId).toBe(b.sessionId);
  });

  it("中身が改ざんされていれば NG", async () => {
    const c = byId("text_zlib");
    const { meta } = { meta: parseFrame(b64(c.meta))! };
    const text = new TextDecoder().decode(meta.payload).replace(/"sha256_raw":"./, '"sha256_raw":"0');
    const payload = new TextEncoder().encode(text);
    const asm = new Assembler();
    const frames = c.data.map((d) => parseFrame(b64(d))!);
    const r = await new Promise<CompletionResult>((resolve) => {
      asm.onComplete = resolve;
      asm.feed({ ...meta, payload });
      for (const f of frames) asm.feed(f);
    });
    expect(r.ok).toBe(false);
    expect(r.message).toContain("SHA-256");
  });
});

describe.skipIf(!ready)("QR 画像の読み取り（zxing-wasm）", () => {
  beforeAll(() => useLocalWasm());

  it("Python 版が表示する QR を読み、ファイルを復元できる", async () => {
    const info = readJson<{ count: number; rawSha256: string; name: string }>("qr.json");
    const frames: Uint8Array[] = [];
    for (let i = 0; i < info.count; i++) {
      const png = new Uint8Array(readFileSync(join(FIXTURES, `qr_${String(i).padStart(2, "0")}.png`)));
      const dets = await decodeImage(png);
      expect(dets.length).toBe(1);
      expect(dets[0].bytes).not.toBeNull();
      frames.push(dets[0].bytes!);
    }
    const r = await receive(new Assembler(), frames);
    expect(r.ok, r.message).toBe(true);
    expect(r.file!.fileName).toBe(info.name);
    expect(await sha256Hex(r.file!.data)).toBe(info.rawSha256);
  });

  it("ボケた画像はシャープ化で読める", async () => {
    const info = readJson<{ width: number; height: number; frame: string }>("blur.json");
    const rgba = new Uint8ClampedArray(readFileSync(join(FIXTURES, "blur.bin")));
    const img = new ImageData(rgba, info.width, info.height);
    const plain = await decodeImage(img);
    expect(plain.some((d) => d.bytes)).toBe(false); // 素のままでは読めない条件であること
    const reader = new RobustReader();
    let got: Uint8Array | null = null;
    for (let i = 0; i < 3 && !got; i++) got = (await reader.read(img)).find((d) => d.bytes)?.bytes ?? null;
    expect(got).toEqual(b64(info.frame));
    expect(reader.rescued).toBeGreaterThan(0);
  });

  it("QR の周辺だけを渡しても読め、位置は映像全体の座標で返る", async () => {
    const info = readJson<{ width: number; height: number; frame: string }>("blur.json");
    const img = new ImageData(new Uint8ClampedArray(readFileSync(join(FIXTURES, "blur.bin"))), info.width, info.height);
    const full = (await new RobustReader().read(img)).find((d) => d.bytes)!;
    const [x0, y0] = [12, 20]; // 余白の一部を切り落とす
    const [x1, y1] = [img.width, img.height];
    const part = (await new RobustReader().read(crop(img, x0, y0, x1, y1), x0, y0, true)).find((d) => d.bytes)!;
    expect(part.bytes).toEqual(b64(info.frame));
    part.points.forEach(([x, y], i) => {
      expect(Math.abs(x - full.points[i][0])).toBeLessThan(4);
      expect(Math.abs(y - full.points[i][1])).toBeLessThan(4);
    });
  });
});

describe("tar", () => {
  it("fixtures が無くても読み取り器自体は動く", () => {
    expect(readTar(new Uint8Array(0)).entries).toEqual([]);
  });
});
