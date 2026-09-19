/**
 * カメラの映像を取り込み、読み取り用のスレッド（Web Worker）に 1 枚ずつ渡す。
 * 読み取り用のスレッドは 2 つまで並べ、空いている方に新しい映像を渡す（1 秒あたりの読み取り回数を増やす）。
 * 全部が読み取り中なら、その映像は読まない（常に新しい映像だけを読む）。
 *
 * QR の位置がわかっている間は、その周辺だけを切り出して渡す（全体を読むより軽く、読み取り回数を保てる。
 * 端末が熱くなって処理が遅くなっても、表示の切り替えに追いつけるように）。見失ったら全体から探し直す。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { DecodeRequest, DecodeResponse } from "./decodeWorker";
import { type Roi, type Tracked, nextRoi } from "./lib/roi";

const MAX_SIDE = 1920; // これより大きい映像は縮小して読む（読み取り時間を抑える）
const FAST_FPS = 60; // 高速モードで頼むフレームレート
const STALL_MS = 200; // 映像フレームの通知がこれだけ来なければ、一定間隔の読み取りで補う

/** 読み取り用のスレッドの数。CPU のコアが少ない端末では 1 つ（並べても速くならず、画面の動きが重くなる） */
const DECODE_WORKERS = (navigator.hardwareConcurrency ?? 2) >= 4 ? 2 : 1;

/**
 * カメラの撮り方。
 * - quality: 高解像度（1920×1080）。QR を複数並べたときや、離れて撮るとき向け。iPhone では 30fps になる
 * - speed: 高速（1280×720・60fps）。1 マスが粗くなる代わりに、撮影 1 枚の時間が短く、表示の切り替わりが
 *   混ざった画像が減る。iOS は 60fps を「希望」で頼むと 30fps にするので「必須」で頼み、断られたら希望で頼み直す
 */
export type CameraMode = "quality" | "speed";

const MODE_KEY = "qrtransfer.cameraMode";

function loadMode(): CameraMode {
  try {
    return localStorage.getItem(MODE_KEY) === "speed" ? "speed" : "quality";
  } catch {
    return "quality";
  }
}

function saveMode(mode: CameraMode): void {
  try {
    localStorage.setItem(MODE_KEY, mode);
  } catch {
    /* 保存できなくても動作には関係ない */
  }
}

/** 映像を取得する。高速モードでは 60fps を必須として頼み、断られたら希望として頼み直す。 */
async function openCamera(base: MediaTrackConstraints, mode: CameraMode): Promise<MediaStream> {
  if (mode === "speed") {
    const video = { ...base, width: { ideal: 1280 }, height: { ideal: 720 } };
    try {
      return await navigator.mediaDevices.getUserMedia({ video: { ...video, frameRate: { exact: FAST_FPS } }, audio: false });
    } catch (e) {
      // 権限の拒否などはそのまま伝える。条件を満たせないときだけ希望として頼み直す
      // （Safari の OverconstrainedError は DOMException ではないことがあるので、名前で見分ける）
      const name = (e as { name?: string }).name;
      if (name !== "OverconstrainedError" && name !== "NotReadableError") throw e;
    }
    return navigator.mediaDevices.getUserMedia({ video: { ...video, frameRate: { ideal: FAST_FPS } }, audio: false });
  }
  return navigator.mediaDevices.getUserMedia({
    video: { ...base, width: { ideal: 1920 }, height: { ideal: 1080 }, frameRate: { ideal: 30 } },
    audio: false,
  });
}

interface DecodeWorker {
  worker: Worker;
  busy: boolean;
  rescued: number;
}

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
  /** カメラが実際に出しているフレームレート（端末が教えない場合は 0） */
  videoFps: number;
}

export interface CameraDevice {
  deviceId: string;
  label: string;
}

type WakeLockSentinelLike = { release: () => Promise<void> };

export function useScanner(onFrame: (bytes: Uint8Array) => boolean) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const workersRef = useRef<DecodeWorker[]>([]);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // 映像の新しいフレームの通し番号と、最後に読み取りに回したフレームの番号（同じフレームを 2 回読まないように）
  const frameRef = useRef({ seq: 0, grabbed: -1, at: 0 });
  const runningRef = useRef(false);
  const enhanceRef = useRef(true);
  const onFrameRef = useRef(onFrame);
  const countRef = useRef({ n: 0, t0: performance.now() });
  const wakeRef = useRef<WakeLockSentinelLike | null>(null);
  const timerRef = useRef<number | null>(null);
  const errorsRef = useRef(0);
  const roiRef = useRef<Tracked | null>(null);
  const startRef = useRef<(id?: string | null) => Promise<void>>(async () => undefined);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [overlay, setOverlay] = useState<Overlay | null>(null);
  const [devices, setDevices] = useState<CameraDevice[]>([]);
  const [deviceId, setDeviceId] = useState<string | null>(null);
  const [stats, setStats] = useState<ScannerStats>({ decodesPerSec: 0, rescued: 0, videoWidth: 0, videoHeight: 0, videoFps: 0 });
  const [mode, setModeState] = useState<CameraMode>(loadMode);
  const modeRef = useRef(mode);
  onFrameRef.current = onFrame;

  const setEnhance = useCallback((on: boolean) => {
    enhanceRef.current = on;
  }, []);

  const grab = useCallback(() => {
    const video = videoRef.current;
    const frame = frameRef.current;
    if (!runningRef.current || !video || video.readyState < 2 || frame.grabbed === frame.seq) return;
    const idle = workersRef.current.find((w) => !w.busy);
    if (!idle) return;
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
    idle.busy = true;
    frame.grabbed = frame.seq;
    const req: DecodeRequest = { id: 0, image, x0, y0, frameWidth: w, frameHeight: h, enhance: enhanceRef.current };
    idle.worker.postMessage(req, [image.data.buffer]);
  }, []);

  const loop = useCallback(() => {
    const video = videoRef.current;
    if (!runningRef.current || !video) return;
    frameRef.current.seq++;
    frameRef.current.at = performance.now();
    grab();
    const v = video as HTMLVideoElement & { requestVideoFrameCallback?: (cb: () => void) => number };
    if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(loop);
    else requestAnimationFrame(loop);
  }, [grab]);

  /** 読み取り結果（どのスレッドからでも）を受信処理に渡し、読む範囲と表示を更新する */
  const onResult = useCallback((res: DecodeResponse) => {
    const boxes = res.detections.map((d) => ({ points: d.points, ok: d.bytes ? onFrameRef.current(d.bytes) : false }));
    // 読む範囲は、正しく読めた QR の位置から決める（QR でない模様や、無関係の QR の位置に固定されないように）
    roiRef.current = nextRoi(roiRef.current, boxes.filter((b) => b.ok).map((b) => b.points), res.width, res.height);
    if (boxes.length) setOverlay({ width: res.width, height: res.height, boxes, time: performance.now() });
    const c = countRef.current;
    c.n++;
    const now = performance.now();
    if (now - c.t0 >= 1000) {
      const video = videoRef.current;
      setStats({
        decodesPerSec: (c.n * 1000) / (now - c.t0),
        rescued: workersRef.current.reduce((sum, w) => sum + w.rescued, 0),
        videoWidth: video?.videoWidth ?? 0, videoHeight: video?.videoHeight ?? 0,
        videoFps: streamRef.current?.getVideoTracks()[0]?.getSettings().frameRate ?? 0,
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
  }, []);

  const ensureWorkers = useCallback(() => {
    if (workersRef.current.length) return;
    for (let i = 0; i < DECODE_WORKERS; i++) {
      const worker = new Worker(new URL("./decodeWorker.ts", import.meta.url), { type: "module" });
      const entry: DecodeWorker = { worker, busy: false, rescued: 0 };
      worker.onmessage = (ev: MessageEvent<DecodeResponse>) => {
        entry.busy = false;
        entry.rescued = ev.data.rescued;
        onResult(ev.data);
        grab(); // 読み終わった時点で新しいフレームが届いていれば、すぐに読む
      };
      worker.onerror = (e) => {
        entry.busy = false;
        setError(`読み取り処理でエラーが発生しました: ${e.message}`);
      };
      workersRef.current.push(entry);
    }
  }, [grab, onResult]);

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
        const video: MediaTrackConstraints = id ? { deviceId: { exact: id } } : { facingMode: { ideal: "environment" } };
        const stream = isDemo
          ? await (await import("./demoStream")).demoStream(Number(new URLSearchParams(location.search).get("demo")) || 6)
          : await openCamera(video, modeRef.current);
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
        ensureWorkers();
        runningRef.current = true;
        setRunning(true);
        loop();
        // 映像フレームの通知（requestVideoFrameCallback）が止まる環境でも読み取りが続くよう、止まっていれば一定間隔で読む
        if (timerRef.current === null) {
          timerRef.current = window.setInterval(() => {
            const f = frameRef.current;
            if (performance.now() - f.at < STALL_MS) return;
            f.seq++;
            grab();
          }, 40);
        }
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
    [deviceId, ensureWorkers, grab, loop, releaseStream],
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

  /** カメラの撮り方を変える（この端末のブラウザに覚える）。動作中なら開き直す */
  const setMode = useCallback(
    (m: CameraMode) => {
      modeRef.current = m;
      setModeState(m);
      saveMode(m);
      if (runningRef.current) void start();
    },
    [start],
  );

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
      workersRef.current.forEach((w) => w.worker.terminate());
      workersRef.current = []; // StrictMode の再マウントで、止めた Worker を使い回さないように
    },
    [releaseStream],
  );

  return { videoRef, running, error, overlay, devices, deviceId, stats, mode, workers: DECODE_WORKERS, start, stop, switchDevice, setEnhance, setMode };
}
