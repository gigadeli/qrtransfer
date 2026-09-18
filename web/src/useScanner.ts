/**
 * カメラの映像を取り込み、読み取り用のスレッド（Web Worker）に 1 枚ずつ渡す。
 * 読み取り中は次の画像を渡さない（常に最新の画像だけを読む）。
 *
 * QR の位置がわかっている間は、その周辺だけを切り出して渡す（全体を読むより軽く、読み取り回数を保てる。
 * 端末が熱くなって処理が遅くなっても、表示の切り替えに追いつけるように）。見失ったら全体から探し直す。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { DecodeRequest, DecodeResponse } from "./decodeWorker";
import { roiOf } from "./lib/qr";

const MAX_SIDE = 1920; // これより大きい映像は縮小して読む（読み取り時間を抑える）
const ROI_MISSES = 10; // 周辺だけを読んで、読めない回数がこれだけ続いたら全体から探し直す

type Roi = [number, number, number, number];

export interface Overlay {
  width: number;
  height: number;
  boxes: { points: [number, number][]; ok: boolean }[];
  time: number;
}

export interface ScannerStats {
  decodesPerSec: number;
  rescued: number;
  videoWidth: number;
  videoHeight: number;
}

export interface CameraDevice {
  deviceId: string;
  label: string;
}

type WakeLockSentinelLike = { release: () => Promise<void> };

export function useScanner(onFrame: (bytes: Uint8Array) => boolean) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const workerRef = useRef<Worker | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const busyRef = useRef(false);
  const runningRef = useRef(false);
  const enhanceRef = useRef(true);
  const onFrameRef = useRef(onFrame);
  const countRef = useRef({ n: 0, t0: performance.now() });
  const wakeRef = useRef<WakeLockSentinelLike | null>(null);
  const timerRef = useRef<number | null>(null);
  const errorsRef = useRef(0);
  const roiRef = useRef<{ roi: Roi; w: number; h: number; misses: number } | null>(null);
  const startRef = useRef<(id?: string | null) => Promise<void>>(async () => undefined);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [overlay, setOverlay] = useState<Overlay | null>(null);
  const [devices, setDevices] = useState<CameraDevice[]>([]);
  const [deviceId, setDeviceId] = useState<string | null>(null);
  const [stats, setStats] = useState<ScannerStats>({ decodesPerSec: 0, rescued: 0, videoWidth: 0, videoHeight: 0 });
  onFrameRef.current = onFrame;

  const setEnhance = useCallback((on: boolean) => {
    enhanceRef.current = on;
  }, []);

  const grab = useCallback(() => {
    const video = videoRef.current;
    const worker = workerRef.current;
    if (!runningRef.current || !video || !worker || busyRef.current || video.readyState < 2) return;
    const vw = video.videoWidth;
    const vh = video.videoHeight;
    if (!vw || !vh) return;
    const scale = Math.min(1, MAX_SIDE / Math.max(vw, vh));
    const w = Math.round(vw * scale);
    const h = Math.round(vh * scale);
    const tracked = roiRef.current;
    const [x0, y0, x1, y1]: Roi = tracked && tracked.w === w && tracked.h === h ? tracked.roi : [0, 0, w, h];
    const cw = x1 - x0;
    const ch = y1 - y0;
    const canvas = (canvasRef.current ??= document.createElement("canvas"));
    if (canvas.width < cw || canvas.height < ch) {
      canvas.width = Math.max(canvas.width, w);
      canvas.height = Math.max(canvas.height, h);
    }
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    if (!ctx) return;
    // 映像の必要な部分だけを描いて取り出す（取り出しは iPhone では特に重いので、小さいほど良い）
    ctx.drawImage(video, x0 / scale, y0 / scale, cw / scale, ch / scale, 0, 0, cw, ch);
    const image = ctx.getImageData(0, 0, cw, ch);
    busyRef.current = true;
    const req: DecodeRequest = { id: 0, image, x0, y0, frameWidth: w, frameHeight: h, enhance: enhanceRef.current };
    worker.postMessage(req, [image.data.buffer]);
  }, []);

  const loop = useCallback(() => {
    const video = videoRef.current;
    if (!runningRef.current || !video) return;
    grab();
    const v = video as HTMLVideoElement & { requestVideoFrameCallback?: (cb: () => void) => number };
    if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(loop);
    else requestAnimationFrame(loop);
  }, [grab]);

  const ensureWorker = useCallback(() => {
    if (workerRef.current) return workerRef.current;
    const worker = new Worker(new URL("./decodeWorker.ts", import.meta.url), { type: "module" });
    worker.onmessage = (ev: MessageEvent<DecodeResponse>) => {
      busyRef.current = false;
      const res = ev.data;
      // 読む範囲は、実際に読めた QR の位置から決める（QR でない模様を誤って見つけた位置に固定されないように）
      const found = res.detections.find((d) => d.bytes);
      const tracked = roiRef.current;
      if (found) {
        roiRef.current = { roi: roiOf(found.points, res.width, res.height), w: res.width, h: res.height, misses: 0 };
      } else if (tracked && ++tracked.misses >= ROI_MISSES) {
        roiRef.current = null;
      }
      const boxes = res.detections.map((d) => ({ points: d.points, ok: d.bytes ? onFrameRef.current(d.bytes) : false }));
      if (boxes.length) setOverlay({ width: res.width, height: res.height, boxes, time: performance.now() });
      const c = countRef.current;
      c.n++;
      const now = performance.now();
      if (now - c.t0 >= 1000) {
        const video = videoRef.current;
        setStats({
          decodesPerSec: (c.n * 1000) / (now - c.t0), rescued: res.rescued,
          videoWidth: video?.videoWidth ?? 0, videoHeight: video?.videoHeight ?? 0,
        });
        c.n = 0;
        c.t0 = now;
      }
      if (res.error) {
        console.warn(res.error);
        // 読み取り部品（WebAssembly）の読み込み失敗などは毎回失敗するので、続いたら画面に出す
        if (++errorsRef.current === 10) setError(`読み取り処理でエラーが発生しています: ${res.error}`);
      } else {
        errorsRef.current = 0;
      }
    };
    worker.onerror = (e) => {
      busyRef.current = false;
      setError(`読み取り処理でエラーが発生しました: ${e.message}`);
    };
    workerRef.current = worker;
    return worker;
  }, []);

  const releaseStream = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    roiRef.current = null;
    void wakeRef.current?.release().catch(() => undefined);
    wakeRef.current = null;
  }, []);

  const start = useCallback(
    async (id: string | null = deviceId) => {
      setError(null);
      const isDemo = import.meta.env.DEV && new URLSearchParams(location.search).has("demo");
      if (!isDemo && (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia)) {
        setError("カメラを使うには HTTPS で開いてください。");
        return;
      }
      releaseStream();
      try {
        const video: MediaTrackConstraints = {
          width: { ideal: 1920 },
          height: { ideal: 1080 },
          frameRate: { ideal: 30 },
          ...(id ? { deviceId: { exact: id } } : { facingMode: { ideal: "environment" } }),
        };
        const stream = isDemo
          ? await (await import("./demoStream")).demoStream(Number(new URLSearchParams(location.search).get("demo")) || 6)
          : await navigator.mediaDevices.getUserMedia({ video, audio: false });
        streamRef.current = stream;
        const track = stream.getVideoTracks()[0];
        track.addEventListener("ended", () => {
          // 他のアプリがカメラを使った・端末がカメラを止めた。表示中なら開き直す（非表示なら戻ったときに開き直す）
          if (streamRef.current !== stream || !runningRef.current) return;
          if (document.visibilityState === "visible") void startRef.current();
        });
        try {
          await track.applyConstraints({ advanced: [{ focusMode: "continuous" } as MediaTrackConstraintSet] });
        } catch {
          /* 対応していないカメラでは無視 */
        }
        const el = videoRef.current!;
        el.srcObject = stream;
        el.setAttribute("playsinline", "true");
        el.muted = true;
        await el.play();
        ensureWorker();
        runningRef.current = true;
        setRunning(true);
        loop();
        // 映像フレームの通知（requestVideoFrameCallback）が止まる環境でも読み取りが続くよう、一定間隔でも試す
        if (timerRef.current === null) timerRef.current = window.setInterval(grab, 80);
        const current = track.getSettings().deviceId ?? null;
        const list = (await (navigator.mediaDevices?.enumerateDevices() ?? Promise.resolve([])))
          .filter((d) => d.kind === "videoinput")
          .map((d, i) => ({ deviceId: d.deviceId, label: d.label || `カメラ ${i + 1}` }));
        setDevices(list);
        setDeviceId(current);
        try {
          const wl = (navigator as Navigator & { wakeLock?: { request: (t: "screen") => Promise<WakeLockSentinelLike> } }).wakeLock;
          wakeRef.current = (await wl?.request("screen")) ?? null; // 受信中に画面が消えないようにする
        } catch {
          /* 対応していなければ無視 */
        }
      } catch (e) {
        const name = (e as DOMException).name;
        setError(
          name === "NotAllowedError"
            ? "カメラの使用が許可されていません。ブラウザの設定でカメラを許可してください。"
            : `カメラを開けませんでした（${name || e}）`,
        );
        runningRef.current = false;
        setRunning(false);
        releaseStream();
      }
    },
    [deviceId, ensureWorker, grab, loop, releaseStream],
  );

  startRef.current = start;

  // アプリの切り替えや画面ロックから戻ったとき: 止まったカメラを開き直し、画面を消さない設定も取り直す
  useEffect(() => {
    const onVisible = async () => {
      if (document.visibilityState !== "visible" || !runningRef.current) return;
      const track = streamRef.current?.getVideoTracks()[0];
      if (!track || track.readyState === "ended") {
        await startRef.current();
        return;
      }
      try {
        const wl = (navigator as Navigator & { wakeLock?: { request: (t: "screen") => Promise<WakeLockSentinelLike> } }).wakeLock;
        wakeRef.current = (await wl?.request("screen")) ?? null;
      } catch {
        /* 対応していなければ無視 */
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, []);

  const stop = useCallback(() => {
    runningRef.current = false;
    setRunning(false);
    setOverlay(null);
    releaseStream();
    if (videoRef.current) videoRef.current.srcObject = null;
  }, [releaseStream]);

  const switchDevice = useCallback(
    (id: string) => {
      setDeviceId(id);
      void start(id);
    },
    [start],
  );

  useEffect(
    () => () => {
      runningRef.current = false;
      releaseStream();
      workerRef.current?.terminate();
      workerRef.current = null; // StrictMode の再マウントで、止めた Worker を使い回さないように
    },
    [releaseStream],
  );

  return { videoRef, running, error, overlay, devices, deviceId, stats, start, stop, switchDevice, setEnhance };
}
