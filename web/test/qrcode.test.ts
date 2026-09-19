import { beforeAll, describe, expect, it } from "vitest";
import { decodeImage } from "../src/lib/qr";
import { type Ecc, type QrMatrix, capacity, encodeQr, packModules } from "../src/lib/qrcode";
import { useLocalWasm } from "./helpers";

// Python 版（segno）の容量表（qrgen.capacity の値）
const PY_CAPACITY: Record<Ecc, number[]> = {"l": [17, 32, 53, 78, 106, 134, 154, 192, 230, 271, 321, 367, 425, 458, 520, 586, 644, 718, 792, 858, 929, 1003, 1091, 1171, 1273, 1367, 1465, 1528, 1628, 1732, 1840, 1952, 2068, 2188, 2303, 2431, 2563, 2699, 2809, 2953], "m": [14, 26, 42, 62, 84, 106, 122, 152, 180, 213, 251, 287, 331, 362, 412, 450, 504, 560, 624, 666, 711, 779, 857, 911, 997, 1059, 1125, 1190, 1264, 1370, 1452, 1538, 1628, 1722, 1809, 1911, 1989, 2099, 2213, 2331], "q": [11, 20, 32, 46, 60, 74, 86, 108, 130, 151, 177, 203, 241, 258, 292, 322, 364, 394, 442, 482, 509, 565, 611, 661, 715, 751, 805, 868, 908, 982, 1030, 1112, 1168, 1228, 1283, 1351, 1423, 1499, 1579, 1663], "h": [7, 14, 24, 34, 44, 58, 64, 84, 98, 119, 137, 155, 177, 194, 220, 250, 280, 310, 338, 382, 403, 439, 461, 511, 535, 593, 625, 658, 698, 742, 790, 842, 898, 958, 983, 1051, 1093, 1139, 1219, 1273]};

/** QR を画像にする（余白 4 マス、1 マス scale ピクセル） */
export function toImage(qr: QrMatrix, scale = 3): ImageData {
  const border = 4;
  const side = (qr.size + border * 2) * scale;
  const img = new ImageData(side, side);
  img.data.fill(255);
  for (let y = 0; y < qr.size; y++) {
    for (let x = 0; x < qr.size; x++) {
      if (!qr.modules[y * qr.size + x]) continue;
      for (let dy = 0; dy < scale; dy++) {
        for (let dx = 0; dx < scale; dx++) {
          const p = (((y + border) * scale + dy) * side + (x + border) * scale + dx) * 4;
          img.data[p] = img.data[p + 1] = img.data[p + 2] = 0;
        }
      }
    }
  }
  return img;
}

function randomBytes(n: number, seed: number): Uint8Array {
  const out = new Uint8Array(n);
  let s = seed;
  for (let i = 0; i < n; i++) {
    s = (Math.imul(s, 1103515245) + 12345) >>> 0;
    out[i] = s >>> 24;
  }
  return out;
}

describe("QR の生成", () => {
  beforeAll(() => useLocalWasm());

  it("容量が Python 版と一致する", () => {
    for (const ecc of ["l", "m", "q", "h"] as Ecc[]) {
      expect(Array.from({ length: 40 }, (_, i) => capacity(i + 1, ecc))).toEqual(PY_CAPACITY[ecc]);
    }
  });

  const cases: [number, Ecc][] = [
    [1, "l"], [2, "m"], [5, "q"], [7, "h"], [9, "l"], [10, "m"], [14, "l"], [20, "q"], [25, "l"], [27, "m"], [32, "l"], [40, "l"], [40, "h"],
  ];
  it.each(cases)("バージョン %i・誤り訂正 %s を容量いっぱいまで入れて、読み戻せる", async (version, ecc) => {
    const data = randomBytes(capacity(version, ecc), version * 7 + ecc.charCodeAt(0));
    const qr = encodeQr(data, version, ecc);
    expect(qr.size).toBe(version * 4 + 17);
    const found = await decodeImage(toImage(qr, version > 30 ? 2 : 3));
    expect(found.length).toBe(1);
    expect(found[0].bytes).toEqual(data);
  });

  it.each([0, 3, 7])("マスクを固定しても読み戻せる（マスク %i）", async (mask) => {
    for (const [version, ecc] of [[20, "l"], [27, "m"], [40, "l"]] as [number, Ecc][]) {
      const data = randomBytes(capacity(version, ecc), version + mask);
      const found = await decodeImage(toImage(encodeQr(data, version, ecc, mask), version > 30 ? 2 : 3));
      expect(found[0]?.bytes).toEqual(data);
    }
  });

  it("モジュールを 1 ビットずつに詰める", () => {
    const m = new Uint8Array([1, 0, 1, 1, 0, 0, 0, 1, 1]);
    expect([...packModules(m)]).toEqual([0b10110001, 0b10000000]);
  });

  it("短いデータでも、指定したバージョンのまま作る", async () => {
    const data = new TextEncoder().encode("QZ");
    const qr = encodeQr(data, 12, "l");
    expect(qr.size).toBe(12 * 4 + 17);
    expect((await decodeImage(toImage(qr)))[0].bytes).toEqual(data);
  });

  it("容量を超えるとエラー", () => {
    expect(() => encodeQr(new Uint8Array(capacity(3, "m") + 1), 3, "m")).toThrow(RangeError);
  });
});
