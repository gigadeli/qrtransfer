/**
 * 敵対的レビューで見つかった問題の再発防止テスト。
 * 悪意のある（または壊れた）送信側から来るフレーム・アーカイブで、ページが固まったり、黙ってデータを失ったりしないこと。
 */
import { describe, expect, it } from "vitest";
import { makeZip, readTar } from "../src/lib/archive";
import { Assembler, type CompletionResult, mimeOf, rangesOfZeros } from "../src/lib/assembler";
import { crc32 } from "../src/lib/crc32";
import { formatRanges, parseFrame } from "../src/lib/protocol";
import { type FixtureCase, b64, fixturesReady, readJson } from "./helpers";

function frame(type: number, sid: number, seq: number, total: number, payload: Uint8Array): Uint8Array {
  const out = new Uint8Array(20 + payload.length);
  const v = new DataView(out.buffer);
  out.set([0x51, 0x5a, 1, type]);
  v.setUint32(4, sid);
  v.setUint32(8, seq);
  v.setUint32(12, total);
  out.set(payload, 16);
  v.setUint32(16 + payload.length, crc32(out, 0, 16 + payload.length));
  return out;
}

function tarOf(files: [string, string][]): Uint8Array {
  const enc = new TextEncoder();
  const blocks: Uint8Array[] = [];
  for (const [name, content] of files) {
    const data = enc.encode(content);
    const h = new Uint8Array(512);
    h.set(enc.encode(name).subarray(0, 100), 0);
    h.set(enc.encode("0000644\0"), 100);
    h.set(enc.encode(data.length.toString(8).padStart(11, "0") + "\0"), 124);
    h.set(enc.encode("00000000000\0"), 136);
    h[156] = 0x30;
    h.set(enc.encode("ustar\0"), 257);
    const body = new Uint8Array(Math.ceil(data.length / 512) * 512);
    body.set(data);
    blocks.push(h, body);
  }
  blocks.push(new Uint8Array(1024));
  const out = new Uint8Array(blocks.reduce((n, b) => n + b.length, 0));
  let o = 0;
  for (const b of blocks) {
    out.set(b, o);
    o += b.length;
  }
  return out;
}

function receive(asm: Assembler, frames: Uint8Array[]): Promise<CompletionResult> {
  return new Promise((resolve) => {
    asm.onComplete = resolve;
    for (const f of frames) asm.feed(parseFrame(f)!);
  });
}

describe("大きさの上限（偽造したフレームで巨大な領域を確保させない）", () => {
  it("総チャンク数が上限を超えるフレームは受け付けず、セッションも作らない", () => {
    const asm = new Assembler();
    const t0 = performance.now();
    expect(asm.feed(parseFrame(frame(1, 1, 0, 0xffffffff, new Uint8Array(300)))!)).toBe("too_large");
    const s = asm.snapshot();
    asm.missingText(200);
    expect(performance.now() - t0).toBeLessThan(200);
    expect(s.sessionId).toBeNull();
    expect(s.rejected?.sessionId).toBe(1);
    // 同じセッションの以後のフレームも無視する。別の（正常な）転送は受信できる
    expect(asm.feed(parseFrame(frame(1, 1, 1, 0xffffffff, new Uint8Array(300)))!)).toBe("too_large");
    expect(asm.feed(parseFrame(frame(1, 2, 0, 3, new Uint8Array(300)))!)).toBe("new");
    expect(asm.snapshot().sessionId).toBe(2);
    expect(asm.snapshot().rejected).toBeNull();
  });

  it("受信中に大きすぎる別の転送が来ても、切り替え候補にしない", () => {
    const asm = new Assembler();
    asm.feed(parseFrame(frame(1, 1, 0, 3, new Uint8Array(300)))!);
    expect(asm.feed(parseFrame(frame(1, 9, 0, 0x7fffffff, new Uint8Array(300)))!)).toBe("too_large");
    expect(asm.snapshot().conflictSessionId).toBeNull();
    expect(asm.switchToConflict()).toBe(false);
    expect(asm.snapshot().sessionId).toBe(1);
  });

  it("上限いっぱいの総チャンク数でも、欠落番号の表示は軽い", () => {
    const asm = new Assembler();
    const total = 500_000;
    asm.feed(parseFrame(frame(1, 1, 5, total, new Uint8Array(300)))!);
    const t0 = performance.now();
    const text = asm.missingText(200);
    asm.snapshot();
    expect(performance.now() - t0).toBeLessThan(200);
    expect(text.startsWith("0-4,6-499999")).toBe(true);
  });

  it("META のサイズが上限を超える転送は、名前付きで知らせて受け付けない", () => {
    const asm = new Assembler({ maxRawSize: 1000, maxPayloadSize: 1000 });
    const meta = {
      name: "big.iso", mode: "file", compression: "none", raw_size: 5000, payload_size: 600, chunk_size: 300,
      total: 2, sha256_raw: "0".repeat(64), sha256_payload: "0".repeat(64),
    };
    asm.feed(parseFrame(frame(1, 5, 0, 2, new Uint8Array(300)))!);
    expect(asm.feed(parseFrame(frame(0, 5, 0, 2, new TextEncoder().encode(JSON.stringify(meta))))!)).toBe("too_large");
    const s = asm.snapshot();
    expect(s.sessionId).toBeNull();
    expect(s.rejected).toMatchObject({ sessionId: 5, name: "big.iso", size: 5000, limit: 1000 });
    expect(asm.feed(parseFrame(frame(1, 5, 1, 2, new Uint8Array(300)))!)).toBe("too_large");
  });
});

describe.skipIf(!fixturesReady())("伸長爆弾", () => {
  const cases = fixturesReady() ? readJson<{ cases: FixtureCase[] }>("frames.json").cases : [];
  for (const id of ["bomb_lzma", "bomb_zlib"]) {
    it(`${id}: META の値（1MB）を超えた時点で伸長を打ち切って NG にする`, async () => {
      const c = cases.find((x) => x.id === id)!;
      const t0 = performance.now();
      const r = await receive(new Assembler(), [c.meta, ...c.data].map(b64));
      expect(r.ok).toBe(false);
      expect(r.message).toContain("伸長後のサイズ");
      expect(performance.now() - t0).toBeLessThan(5000);
    });
  }
});

describe("ZIP・ファイル名", () => {
  it("65,536 件以上でも件数が壊れない（ZIP64 の終端レコードを付ける）", () => {
    const n = 70_000;
    const entries = Array.from({ length: n }, (_, i) => ({ path: `f${i}.txt`, dir: false, data: new Uint8Array([65]), mtime: 0 }));
    const zip = makeZip(entries);
    const v = new DataView(zip.buffer);
    const eocd = zip.length - 22;
    expect(v.getUint32(eocd, true)).toBe(0x06054b50);
    expect(v.getUint16(eocd + 10, true)).toBe(0xffff);
    const loc = eocd - 20;
    expect(v.getUint32(loc, true)).toBe(0x07064b50);
    const rec = Number(v.getBigUint64(loc + 8, true));
    expect(v.getUint32(rec, true)).toBe(0x06064b50);
    expect(Number(v.getBigUint64(rec + 32, true))).toBe(n);
    // 中央ディレクトリの位置と大きさが実際と合っている
    const cdOffset = Number(v.getBigUint64(rec + 48, true));
    const cdSize = Number(v.getBigUint64(rec + 40, true));
    expect(v.getUint32(cdOffset, true)).toBe(0x02014b50);
    expect(cdOffset + cdSize).toBe(rec);
  });

  it("65,535 件以下では従来の形式のまま", () => {
    const zip = makeZip([{ path: "a.txt", dir: false, data: new Uint8Array([1]), mtime: 0 }]);
    const v = new DataView(zip.buffer);
    expect(v.getUint16(zip.length - 22 + 10, true)).toBe(1);
    expect(v.getUint32(zip.length - 42, true)).not.toBe(0x07064b50);
  });

  it("名前を直した結果重なるファイルは、連番を付けて残す（黙って捨てない）", () => {
    const t = readTar(tarOf([["ab?.txt", "1"], ["ab_.txt", "2"], ["CON.txt", "3"], ["_CON.txt", "4"], ["Readme", "5"], ["README", "6"]]));
    expect(t.entries.map((e) => e.path)).toEqual(["ab_.txt", "ab_ (2).txt", "_CON.txt", "_CON (2).txt", "Readme", "README (2)"]);
    expect(t.entries.map((e) => new TextDecoder().decode(e.data))).toEqual(["1", "2", "3", "4", "5", "6"]);
    expect(t.renamed).toEqual([
      { name: "ab_.txt", to: "ab_ (2).txt" },
      { name: "_CON.txt", to: "_CON (2).txt" },
      { name: "README", to: "README (2)" },
    ]);
    expect(t.skipped).toEqual([]);
  });

  it("同じ名前のファイルとフォルダがあるときは、理由を付けてスキップする", () => {
    const t = readTar(tarOf([["a", "file"], ["a/b.txt", "x"]]));
    expect(t.entries.map((e) => e.path)).toEqual(["a"]);
    expect(t.skipped).toHaveLength(1);
    expect(t.skipped[0].name).toBe("a/b.txt");
  });

  it("ブラウザが開いて実行し得る種類（HTML・SVG など）では共有しない", () => {
    for (const name of ["x.html", "x.htm", "x.svg", "x.xml", "x.xhtml", "x.js"]) expect(mimeOf(name)).toBe("application/octet-stream");
    expect(mimeOf("photo.JPG")).toBe("image/jpeg");
  });
});

describe("欠落番号の表記", () => {
  it("受信済みの印から作る表記は、番号の一覧から作るものと同じ", () => {
    for (let trial = 0; trial < 50; trial++) {
      const bits = new Uint8Array(1 + ((trial * 37) % 300)).map((_, i) => ((i * 7 + trial) % 5 < 2 ? 0 : 1));
      const zeros = [...bits.keys()].filter((i) => !bits[i]);
      expect(rangesOfZeros(bits)).toBe(formatRanges(zeros));
      expect(rangesOfZeros(bits, 3)).toBe(formatRanges(zeros, 3));
    }
  });
});
