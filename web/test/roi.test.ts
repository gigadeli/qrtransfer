import { describe, expect, it } from "vitest";
import { ROI_MISSES, type Tracked, nextRoi } from "../src/lib/roi";

type Pts = [number, number][];
const box = (x: number, y: number, s = 100): Pts => [[x, y], [x + s, y], [x + s, y + s], [x, y + s]];
const W = 1920;
const H = 1080;

describe("読み取る範囲（ROI）の追跡", () => {
  it("読めた QR 全体を囲む範囲にする（複数並んでいるときは全部）", () => {
    const t = nextRoi(null, [box(100, 400), box(300, 400), box(500, 400)], W, H)!;
    expect(t.expected).toBe(3);
    const [x0, y0, x1, y1] = t.roi;
    expect(x0).toBeLessThan(100);
    expect(x1).toBeGreaterThan(600);
    expect(y0).toBeLessThan(400);
    expect(y1).toBeGreaterThan(500);
  });

  it("一部しか読めなかったときは範囲を狭めない。続くようなら全体から探し直す", () => {
    let t: Tracked | null = nextRoi(null, [box(100, 400), box(500, 400)], W, H);
    const roi = t!.roi;
    for (let i = 0; i < ROI_MISSES - 1; i++) {
      t = nextRoi(t, [box(100, 400)], W, H);
      expect(t!.roi).toEqual(roi);
    }
    expect(nextRoi(t, [box(100, 400)], W, H)).toBeNull();
  });

  it("全部読めれば、一部しか読めなかった回数は数え直す", () => {
    let t: Tracked | null = nextRoi(null, [box(100, 400), box(500, 400)], W, H);
    for (let i = 0; i < ROI_MISSES - 1; i++) t = nextRoi(t, [box(100, 400)], W, H);
    t = nextRoi(t, [box(110, 400), box(510, 400)], W, H);
    expect(t!.short).toBe(0);
    expect(nextRoi(t, [box(110, 400)], W, H)).not.toBeNull();
  });

  it("1 つも読めない回数が続いたら全体から探し直す。映像の大きさが変わったら使わない", () => {
    let t: Tracked | null = nextRoi(null, [box(100, 400)], W, H);
    for (let i = 0; i < ROI_MISSES - 1; i++) t = nextRoi(t, [], W, H);
    expect(t).not.toBeNull();
    expect(nextRoi(t, [], W, H)).toBeNull();
    expect(nextRoi(nextRoi(null, [box(100, 400)], W, H), [], 1280, 720)).toBeNull();
  });

  it("QR が 1 つのときは、読めた位置に追従する（従来どおり）", () => {
    const a = nextRoi(null, [box(100, 400)], W, H)!;
    const b = nextRoi(a, [box(700, 200)], W, H)!;
    expect(b.roi[0]).toBeGreaterThan(600);
  });
});
