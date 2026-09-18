/// <reference lib="webworker" />
/** QR の読み取りを別スレッドで行う（画面の描画やカメラの取り込みを止めないため）。 */
import wasmUrl from "zxing-wasm/reader/zxing_reader.wasm?url";
import { RobustReader, configureWasm } from "./lib/qr";

configureWasm(wasmUrl);
const reader = new RobustReader();

export interface DecodeRequest {
  id: number;
  image: ImageData; // 映像全体、または QR の周辺だけ（左上が x0, y0）
  x0: number;
  y0: number;
  frameWidth: number;
  frameHeight: number;
  enhance: boolean;
}

export interface DecodeResponse {
  id: number;
  width: number;
  height: number;
  detections: { points: [number, number][]; bytes: Uint8Array | null }[];
  rescued: number;
  error?: string;
}

self.onmessage = async (ev: MessageEvent<DecodeRequest>) => {
  const { id, image, x0, y0, frameWidth: width, frameHeight: height, enhance } = ev.data;
  reader.enhance = enhance;
  try {
    const focused = image.width < width || image.height < height;
    const detections = await reader.read(image, x0, y0, focused);
    const res: DecodeResponse = { id, width, height, detections, rescued: reader.rescued };
    const transfer = detections.flatMap((d) => (d.bytes ? [d.bytes.buffer as ArrayBuffer] : []));
    (self as unknown as DedicatedWorkerGlobalScope).postMessage(res, transfer);
  } catch (e) {
    const res: DecodeResponse = { id, width, height, detections: [], rescued: reader.rescued, error: String(e) };
    (self as unknown as DedicatedWorkerGlobalScope).postMessage(res);
  }
};
