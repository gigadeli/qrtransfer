/// <reference lib="webworker" />
/** QR の読み取りを別スレッドで行う（画面の描画やカメラの取り込みを止めないため）。 */
import wasmUrl from "zxing-wasm/reader/zxing_reader.wasm?url";
import { RobustReader, configureWasm } from "./lib/qr";

configureWasm(wasmUrl);
const reader = new RobustReader();

export interface DecodeRequest {
  id: number;
  image: ImageData;
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
  const { id, image, enhance } = ev.data;
  reader.enhance = enhance;
  try {
    const detections = await reader.read(image);
    const res: DecodeResponse = { id, width: image.width, height: image.height, detections, rescued: reader.rescued };
    const transfer = detections.flatMap((d) => (d.bytes ? [d.bytes.buffer as ArrayBuffer] : []));
    (self as unknown as DedicatedWorkerGlobalScope).postMessage(res, transfer);
  } catch (e) {
    const res: DecodeResponse = { id, width: image.width, height: image.height, detections: [], rescued: reader.rescued, error: String(e) };
    (self as unknown as DedicatedWorkerGlobalScope).postMessage(res);
  }
};
