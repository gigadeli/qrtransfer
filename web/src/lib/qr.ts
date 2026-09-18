/**
 * zxing-cpp（WebAssembly 版）で QR を読む。デスクトップ版と同じ読み取り部品。
 *
 * 読み取れなかったときは、QR が見つかった位置の周辺だけを「シャープ化」して読み直す
 * （デスクトップ版 RobustDecoder の簡易版。スマホのカメラの軽いボケに効く）。
 */
import { type ReadResult, prepareZXingModule, readBarcodes } from "zxing-wasm/reader";

export interface Detection {
  points: [number, number][];
  bytes: Uint8Array | null; // 読めなかった（誤り訂正の失敗など）ときは null
}

export interface Gray {
  width: number;
  height: number;
  data: Uint8ClampedArray; // RGBA（ImageData と同じ並び）
}

/** WebAssembly ファイルの場所を指定する（既定では CDN から取得するため、同じサーバーから配信する）。 */
export function configureWasm(wasmUrl: string): void {
  prepareZXingModule({
    overrides: {
      locateFile: (path: string, prefix: string) => (path.endsWith(".wasm") ? wasmUrl : prefix + path),
    },
  });
}

const OPTIONS = {
  formats: ["QRCode" as const],
  tryHarder: true,
  tryRotate: true,
  tryInvert: false,
  tryDownscale: true,
  returnErrors: true,
  maxNumberOfSymbols: 1,
};

function toDetections(results: ReadResult[]): Detection[] {
  return results.map((r) => {
    const p = r.position;
    return {
      points: [
        [p.topLeft.x, p.topLeft.y],
        [p.topRight.x, p.topRight.y],
        [p.bottomRight.x, p.bottomRight.y],
        [p.bottomLeft.x, p.bottomLeft.y],
      ],
      bytes: r.isValid && r.bytes?.length ? new Uint8Array(r.bytes) : null,
    };
  });
}

export async function decodeImage(input: ImageData | Uint8Array | Blob): Promise<Detection[]> {
  return toDetections(await readBarcodes(input as ImageData, OPTIONS));
}

/** アンシャープマスク（3×3 の近傍平均との差を強調）。RGBA のまま処理して ImageData を返す。 */
export function sharpen(img: ImageData, amount = 1.5): ImageData {
  const { width: w, height: h, data } = img;
  const gray = new Float32Array(w * h);
  for (let i = 0, j = 0; i < gray.length; i++, j += 4) gray[i] = data[j] * 0.299 + data[j + 1] * 0.587 + data[j + 2] * 0.114;
  const out = new ImageData(w, h);
  const o = out.data;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = y * w + x;
      let v = gray[i];
      if (x > 0 && y > 0 && x < w - 1 && y < h - 1) {
        const blur =
          (gray[i - w - 1] + gray[i - w] + gray[i - w + 1] + gray[i - 1] + v + gray[i + 1] +
            gray[i + w - 1] + gray[i + w] + gray[i + w + 1]) / 9;
        v = v + amount * (v - blur);
      }
      const c = v < 0 ? 0 : v > 255 ? 255 : v;
      const k = i * 4;
      o[k] = o[k + 1] = o[k + 2] = c;
      o[k + 3] = 255;
    }
  }
  return out;
}

export function crop(img: ImageData, x0: number, y0: number, x1: number, y1: number): ImageData {
  const w = x1 - x0;
  const h = y1 - y0;
  const out = new ImageData(w, h);
  for (let y = 0; y < h; y++) {
    const src = ((y0 + y) * img.width + x0) * 4;
    out.data.set(img.data.subarray(src, src + w * 4), y * w * 4);
  }
  return out;
}

/** 位置の周辺（外接矩形の 20% 外側まで）。 */
export function roiOf(points: [number, number][], w: number, h: number): [number, number, number, number] {
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const mx = (x1 - x0) * 0.2 + 8;
  const my = (y1 - y0) * 0.2 + 8;
  return [
    Math.max(0, Math.floor(x0 - mx)), Math.max(0, Math.floor(y0 - my)),
    Math.min(w, Math.ceil(x1 + mx)), Math.min(h, Math.ceil(y1 + my)),
  ];
}

const RETRY_WINDOW = 20; // 読み直しが効いているかを判断する回数
const RETRY_PROBE = 8; // 効いていないときも、この回数に 1 回は試す（ピントが外れてきたときに備えて）

/**
 * 素の画像 → 読めなければ、QR が見つかった位置の周辺をシャープ化して再試行。
 *
 * 読み直しは重い（素の読み取りの 2 倍以上）。QR の切り替わり途中の画像（上下で別の QR が混ざったもの）は
 * 読み直しても読めないので、最近の読み直しが 1 回も成功していなければ、ときどきしか試さない。
 */
export class RobustReader {
  private recent: boolean[] = [];
  private skipped = 0;
  enhance = true;
  rescued = 0;

  /**
   * img は映像の一部（左上が x0, y0）でもよい。返す位置は映像全体の座標。
   * focused: img が、直前に読めた QR の周辺を切り出したもの（QR の位置が見つからなくても、全体を読み直す）。
   */
  async read(img: ImageData, x0 = 0, y0 = 0, focused = false): Promise<Detection[]> {
    const plain = await decodeImage(img);
    if (!plain.some((d) => d.bytes) && (plain.length || focused) && this.enhance && this.shouldRetry()) {
      const [rx0, ry0, rx1, ry1] = plain.length ? roiOf(plain[0].points, img.width, img.height) : [0, 0, img.width, img.height];
      if (rx1 - rx0 > 16 && ry1 - ry0 > 16) {
        const retry = await decodeImage(sharpen(crop(img, rx0, ry0, rx1, ry1)));
        const ok = retry.some((d) => d.bytes);
        this.record(ok);
        if (ok) {
          this.rescued++;
          return offset(retry, x0 + rx0, y0 + ry0);
        }
      }
    }
    return offset(plain, x0, y0);
  }

  private shouldRetry(): boolean {
    if (this.recent.length < RETRY_WINDOW / 2 || this.recent.includes(true)) return true;
    if (++this.skipped < RETRY_PROBE) return false;
    this.skipped = 0;
    return true;
  }

  private record(ok: boolean): void {
    this.recent.push(ok);
    if (this.recent.length > RETRY_WINDOW) this.recent.shift();
  }
}

function offset(dets: Detection[], dx: number, dy: number): Detection[] {
  if (!dx && !dy) return dets;
  return dets.map((d) => ({ ...d, points: d.points.map(([x, y]) => [x + dx, y + dy] as [number, number]) }));
}
