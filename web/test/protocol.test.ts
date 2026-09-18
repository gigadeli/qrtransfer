import { describe, expect, it } from "vitest";
import { makeZip, readTar, sanitizeComponent, splitMemberPath } from "../src/lib/archive";
import { crc32 } from "../src/lib/crc32";
import { formatRanges, parseFrame } from "../src/lib/protocol";

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

describe("protocol", () => {
  it("crc32 は zlib と同じ", () => {
    expect(crc32(new TextEncoder().encode("123456789"))).toBe(0xcbf43926);
  });

  it("フレームを解析し、壊れたものは捨てる", () => {
    const f = frame(1, 0xffffffff, 2, 5, new Uint8Array([1, 2, 3]));
    expect(parseFrame(f)).toMatchObject({ type: 1, sessionId: 0xffffffff, seq: 2, total: 5 });
    const bad = f.slice();
    bad[17] ^= 1;
    expect(parseFrame(bad)).toBeNull();
    expect(parseFrame(frame(1, 1, 5, 5, new Uint8Array([1])))).toBeNull(); // seq >= total
    expect(parseFrame(frame(2, 1, 0, 5, new Uint8Array([1])))).toBeNull(); // 未知の種類
    expect(parseFrame(new Uint8Array(10))).toBeNull();
  });

  it("範囲の表記", () => {
    expect(formatRanges([7, 1, 3, 4, 5])).toBe("1,3-5,7");
    expect(formatRanges([1, 3, 5, 7], 2)).toBe("1,3,…");
    expect(formatRanges([])).toBe("");
  });
});

describe("archive", () => {
  it("危険なパスを拒否し、名前を無害化する", () => {
    for (const bad of ["../a", "/a", "C:/a", "a/../../b", "...", "a/ ./b", ""]) expect(splitMemberPath(bad)).toBeNull();
    expect(splitMemberPath("a/./b\\c")).toEqual(["a", "b", "c"]);
    expect(sanitizeComponent("CON.txt")).toBe("_CON.txt");
    expect(sanitizeComponent('a<b>:"c|?*.')).toBe("a_b___c___");
  });

  it("ZIP を作り直すと中身と CRC が保たれる", () => {
    const enc = new TextEncoder();
    const zip = makeZip([
      { path: "d", dir: true, data: new Uint8Array(0), mtime: 1_700_000_000 },
      { path: "d/日本語.txt", dir: false, data: enc.encode("hello"), mtime: 1_700_000_000 },
    ]);
    const v = new DataView(zip.buffer);
    expect(v.getUint32(0, true)).toBe(0x04034b50);
    expect(v.getUint32(zip.length - 22, true)).toBe(0x06054b50);
    expect(v.getUint16(zip.length - 22 + 10, true)).toBe(2);
    expect(new TextDecoder().decode(zip)).toContain("d/日本語.txt");
    expect(readTar(new Uint8Array(1024)).entries).toEqual([]);
  });
});
