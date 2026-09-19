/**
 * QR を全画面に表示する（パソコン版 fullscreen_qr.py の Web 版）。
 *
 * - 開いた直後は「待機中」: 最初の QR（META）を出したまま待つ。受信側はその間にカメラの位置やピントを合わせる
 * - 並べた QR は 1 つずつ時間をずらして切り替える（1 周期の 1/並べる数 ずつ）。全部を同時に切り替えると、
 *   切り替わりの瞬間にかかった撮影では全部が前後の QR の混ざった画像になるが、ずらせば混ざるのは 1 つだけで済む
 * - 描くときは 1 マス = 整数ピクセルで描き、画面に合わせる残りの拡大はブラウザになめらかに任せる
 *   （半端な倍率でくっきり拡大すると、マスの幅が 1 ピクセルずつばらつき、小さく並べたときに読み取りが落ちる）
 * - QR は Worker で少し先まで作っておく。作るのが間に合わないときは、飛ばさずに待つ（取りこぼしを増やさない）。
 *   1 周分が保持の上限に収まる転送では、作った QR を全部とっておき、2 周目からは作り直さない
 * - 操作はパソコン版と同じキー（Space 開始・一時停止、←→ 1 枚ずつ、+/− 速さ、C 数、R 再送、F 全画面、Esc 終了）
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { CAROUSEL_META, QUIET_ZONE, carouselOrder, gridLayout, modulesWithBorder, repairIndex } from "../lib/carousel";
import { formatRanges, parseRanges } from "../lib/protocol";
import { type Ecc, capacity } from "../lib/qrcode";
import type { FrameClient } from "./client";
import type { PlanInfo } from "./frameWorker";
import { FlipScheduler } from "./scheduler";
import { CODES_MAX, FPS_MAX, FPS_MIN } from "./settings";

export interface PlayerProps {
  client: FrameClient;
  plan: PlanInfo;
  version: number;
  ecc: Ecc;
  repairs: number;
  fps: number;
  codes: number;
  onClose: (state: { fps: number; codes: number }) => void;
}

const BATCH = 4; // Worker に一度に頼む QR の数
const MIN_AHEAD = 8;
const KEEP_ALL_BYTES = 48 * 1024 * 1024; // 1 周分の QR（1 ビットずつに詰めたもの）がこれに収まれば、全部とっておく
const STALL_MS = 1000; // 画面の書き換えがこれより長く止まったら知らせる
const RETRY_MS = 300; // QR を作れなかったときに頼み直すまでの時間
const UI_INTERVAL_MS = 100; // 下の帯の表示を更新する間隔（切り替えのたびに描き直すと重い）
const WHITE = 0xffffffff;
const BLACK = 0xff000000; // RGBA を 32 ビットで見たときの不透明の黒（リトルエンディアン）

function entryLabel(e: number, total: number, repairs: number, short: boolean): string {
  if (e === CAROUSEL_META) return "META";
  const r = repairIndex(e);
  if (r !== null) return short ? `修復 ${r}` : `修復 ${r}（0〜${repairs - 1}）`;
  return short ? `DATA ${e}` : `DATA ${e}（0〜${total - 1}）`;
}

function requestFullscreen(): void {
  // 全画面にできない環境（iPhone の Safari など）では、ページいっぱいに表示するだけ
  document.documentElement.requestFullscreen?.().catch(() => undefined);
}

interface Layout {
  cols: number;
  rows: number;
  scale: number; // 1 マスのピクセル数（整数）
  full: number; // 余白を含む 1 辺のマス数
}

export function Player({ client, plan, version, ecc, repairs, fps: fps0, codes: codes0, onClose }: PlayerProps) {
  const areaRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [fps, setFps] = useState(fps0);
  const [codes, setCodes] = useState(codes0);
  const [waiting, setWaiting] = useState(true);
  const [paused, setPaused] = useState(false);
  const [resend, setResend] = useState<number[] | null>(null); // 再送中の番号
  const [dialog, setDialog] = useState(false);
  const [ui, setUi] = useState({ cells: [0], next: 1, cycle: 1 });
  const [stalled, setStalled] = useState(false);
  const [freeze, setFreeze] = useState<number | null>(null); // 表示が止まっていた秒数（知らせる間だけ）
  const [fullscreen, setFullscreen] = useState(!!document.fullscreenElement);

  // アニメーションの中で使う値（React の再描画を待たずに読む）
  const initialOrder = useRef<Int32Array | null>(null);
  initialOrder.current ??= carouselOrder(plan.total, null, repairs);
  const s = useRef({
    order: initialOrder.current,
    sched: new FlipScheduler(initialOrder.current.length, codes0), // 並べた QR の切り替えの予定
    fps: fps0,
    codes: codes0,
    running: false,
    lastFrame: 0,
    lastUi: 0,
    cache: new Map<number, Uint8Array>(),
    requested: new Set<number>(),
    inflight: 0,
    generation: 0, // 表示順を変えたら増やす（古い依頼の結果を捨てる）
    size: 0, // QR の 1 辺のマス数（余白を除く）
    keepAll: false,
    layout: null as Layout | null,
    closed: false, // 送信画面を閉じた（あとから届いた依頼の結果・頼み直しで、Worker を使わない）
  });

  const shown = () => Math.min(s.current.codes, s.current.order.length);
  const at = (p: number) => ((p % s.current.order.length) + s.current.order.length) % s.current.order.length;

  /** 表示中と、これから出す少し先までの QR を Worker に頼む */
  const fill = useCallback(() => {
    const st = s.current;
    if (st.closed) return;
    const len = st.order.length;
    const ahead = Math.min(len, Math.max(MIN_AHEAD, Math.ceil(st.fps * st.codes * 1.5), st.codes * 2));
    const { cells, nextPos } = st.sched;
    if (!st.keepAll) {
      const keep = new Set(cells);
      for (let i = 0; i < ahead; i++) keep.add(at(nextPos + i));
      for (const p of st.cache.keys()) if (!keep.has(p)) st.cache.delete(p);
    }
    while (st.inflight < 2) {
      const want: number[] = [];
      for (const p of cells) if (!st.cache.has(p) && !st.requested.has(p) && want.length < BATCH) want.push(p);
      for (let i = 0; i < ahead && want.length < BATCH; i++) {
        const p = at(st.sched.nextPos + i);
        if (!st.cache.has(p) && !st.requested.has(p) && !want.includes(p)) want.push(p);
      }
      if (!want.length) return;
      want.forEach((p) => st.requested.add(p));
      st.inflight++;
      const gen = st.generation;
      client
        .request<"rendered">({ type: "render", version, ecc, metaMax: capacity(version, ecc), entries: want.map((p) => st.order[p]) })
        .then((res) => {
          if (gen !== st.generation) return;
          st.size = res.size;
          st.keepAll = st.order.length * res.matrices[0].length <= KEEP_ALL_BYTES;
          want.forEach((p, i) => {
            st.requested.delete(p);
            st.cache.set(p, res.matrices[i]);
          });
        })
        .then(
          () => {
            if (gen === st.generation) st.inflight--;
            fill();
          },
          () => {
            // 作れなかった（Worker を作り直したなど）。すぐに頼み直すと空回りするので、少し待つ
            if (gen === st.generation) {
              st.inflight--;
              want.forEach((p) => st.requested.delete(p));
            }
            window.setTimeout(fill, RETRY_MS);
          },
        );
    }
  }, [client, version, ecc]);

  // 1 つの QR を 1 マス = 1 ピクセルで描く下書き（描くときに整数倍に拡大する）
  const scratch = useRef<{ canvas: HTMLCanvasElement; img: ImageData; px: Uint32Array } | null>(null);

  /** 画面の大きさと並べる数から、キャンバスの大きさ（整数倍）と表示の大きさ（なめらかに拡大）を決める */
  const relayout = useCallback(() => {
    const area = areaRef.current;
    const canvas = canvasRef.current;
    const st = s.current;
    if (!area || !canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.floor(area.clientWidth * dpr));
    const h = Math.max(1, Math.floor(area.clientHeight * dpr));
    const full = modulesWithBorder(version);
    const [cols, rows] = gridLayout(shown(), w, h, full);
    const scale = Math.max(1, Math.floor(Math.min(w / cols, h / rows) / full));
    st.layout = { cols, rows, scale, full };
    canvas.width = cols * full * scale;
    canvas.height = rows * full * scale;
    const stretch = Math.min(w / canvas.width, h / canvas.height);
    canvas.style.width = `${(canvas.width * stretch) / dpr}px`;
    canvas.style.height = `${(canvas.height * stretch) / dpr}px`;
    const ctx = canvas.getContext("2d", { alpha: false })!;
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }, [version]);

  /** 並べた位置 i の QR を描く（その場所だけ描き直す） */
  const drawCell = useCallback((i: number) => {
    const st = s.current;
    const canvas = canvasRef.current;
    const lay = st.layout;
    const bits = st.cache.get(st.sched.cells[i]);
    if (!canvas || !lay || !bits || !st.size) return;
    const { full, scale, cols } = lay;
    if (!scratch.current || scratch.current.img.width !== full) {
      const c = document.createElement("canvas");
      c.width = c.height = full;
      const img = new ImageData(full, full);
      scratch.current = { canvas: c, img, px: new Uint32Array(img.data.buffer) };
    }
    const { canvas: sc, img, px } = scratch.current;
    px.fill(WHITE);
    const size = st.size;
    for (let y = 0, k = 0; y < size; y++) {
      const row = (y + QUIET_ZONE) * full + QUIET_ZONE;
      for (let x = 0; x < size; x++, k++) if ((bits[k >>> 3] >>> (7 - (k & 7))) & 1) px[row + x] = BLACK;
    }
    sc.getContext("2d")!.putImageData(img, 0, 0);
    const ctx = canvas.getContext("2d", { alpha: false })!;
    ctx.imageSmoothingEnabled = false;
    const cell = full * scale;
    ctx.drawImage(sc, (i % cols) * cell, Math.floor(i / cols) * cell, cell, cell);
  }, []);

  const drawAll = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d", { alpha: false })!;
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    s.current.sched.cells.forEach((_, i) => drawCell(i));
  }, [drawCell]);

  const publish = useCallback((force = false) => {
    const st = s.current;
    const now = performance.now();
    if (!force && now - st.lastUi < UI_INTERVAL_MS) return;
    st.lastUi = now;
    setUi({ cells: [...st.sched.cells], next: st.sched.nextPos, cycle: st.sched.cycle });
  }, []);

  /** 表示中の QR が全部そろったら描く */
  const drawWhenReady = useCallback(() => {
    const st = s.current;
    const gen = st.generation;
    const cells = st.sched.cells;
    const check = () => {
      if (gen !== st.generation || cells !== st.sched.cells) return;
      if (cells.every((p) => st.cache.has(p)) && st.size) {
        drawAll();
      } else {
        window.setTimeout(check, 30);
      }
    };
    check();
  }, [drawAll]);

  /** start から続けて並べ直す（一斉に切り替える。開始・コマ送り・並べる数の変更のとき） */
  const setContiguous = useCallback(
    (start: number) => {
      s.current.sched.reset(start, s.current.codes);
      relayout();
      fill();
      drawWhenReady();
      publish(true);
    },
    [relayout, fill, drawWhenReady, publish],
  );

  // 表示の切り替え（画面の書き換えごとに、次の QR を出す時刻になったかを見る）
  useEffect(() => {
    let raf = 0;
    let lastStall = false;
    let freezeTimer = 0;
    const tick = (now: number) => {
      raf = requestAnimationFrame(tick);
      const st = s.current;
      // 画面の書き換えが長く止まっていた（タブを隠した、ウィンドウが隠れたなど）。受信側の不具合と紛れないよう知らせる
      if (st.running && st.lastFrame && now - st.lastFrame > STALL_MS) {
        setFreeze((now - st.lastFrame) / 1000);
        window.clearTimeout(freezeTimer);
        freezeTimer = window.setTimeout(() => setFreeze(null), 8000);
      }
      st.lastFrame = now;
      if (!st.running) return;
      // 1 回の書き換えで、時刻の来た切り替えを全部行う（fps × 並べる数が画面の書き換え回数を超えることもあるため）。
      // QR の用意が間に合っていないときは、飛ばさずに待つ
      const { flipped, missing } = st.sched.due(now, st.fps, (p) => st.cache.has(p));
      flipped.forEach(drawCell);
      if (missing !== lastStall) {
        lastStall = missing;
        setStalled(missing);
      }
      if (flipped.length) {
        fill();
        publish();
      }
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      window.clearTimeout(freezeTimer);
    };
  }, [drawCell, fill, publish]);

  // 最初の並び（META から）
  useEffect(() => {
    s.current.closed = false;
    setContiguous(0);
    return () => {
      s.current.closed = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 画面の大きさに合わせて並べ直す
  useEffect(() => {
    const area = areaRef.current!;
    const ro = new ResizeObserver(() => {
      relayout();
      drawAll();
    });
    ro.observe(area);
    const onFs = () => setFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", onFs);
    return () => {
      ro.disconnect();
      document.removeEventListener("fullscreenchange", onFs);
    };
  }, [relayout, drawAll]);

  // 送信中は画面を暗くしない
  useEffect(() => {
    let lock: WakeLockSentinel | null = null;
    const acquire = async () => {
      try {
        if (document.visibilityState === "visible") lock = await navigator.wakeLock?.request("screen");
      } catch {
        lock = null;
      }
    };
    void acquire();
    const onVis = () => void acquire();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      void lock?.release().catch(() => undefined);
    };
  }, []);

  const setRunning = (run: boolean) => {
    const st = s.current;
    st.running = run;
    st.sched.begin(performance.now(), st.fps);
    st.lastFrame = 0;
  };

  const start = () => {
    setWaiting(false);
    setPaused(false);
    setRunning(true);
  };

  const togglePause = () => {
    if (waiting) return start();
    const next = !paused;
    setPaused(next);
    setRunning(!next);
  };

  const step = (dir: number) => {
    if (!paused && !waiting) return;
    setContiguous(s.current.sched.base + dir * shown());
  };

  const changeFps = (d: number) => {
    const v = Math.min(FPS_MAX, Math.max(FPS_MIN, s.current.fps + d));
    s.current.fps = v;
    setFps(v);
    fill();
  };

  const changeOrder = (order: Int32Array) => {
    const st = s.current;
    st.generation++;
    st.order = order;
    st.cache.clear();
    st.requested.clear();
    st.inflight = 0;
    st.keepAll = false;
    st.sched = new FlipScheduler(order.length, st.codes);
    if (st.running) st.sched.begin(performance.now(), st.fps); // 最初の切り替えは 1 つ分の間隔の後
    setContiguous(0);
  };

  const cycleCodes = () => {
    const v = (s.current.codes % CODES_MAX) + 1; // 1 → 2 → … → 4 → 1
    s.current.codes = v;
    setCodes(v);
    setContiguous(s.current.sched.base);
  };

  const close = useCallback(() => {
    s.current.running = false;
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined);
    onClose({ fps: s.current.fps, codes: s.current.codes });
  }, [onClose]);

  const toggleFullscreen = () => {
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined);
    else requestFullscreen();
  };

  // キー操作（パソコン版と同じ）
  const keys = useRef<(e: KeyboardEvent) => void>(() => undefined);
  keys.current = (e: KeyboardEvent) => {
    if (dialog) return;
    const k = e.key;
    if (k === "Escape") close();
    else if (waiting && (k === " " || k === "Enter")) start();
    else if (k === " ") togglePause();
    else if (k === "ArrowRight" || k === "ArrowLeft") step(k === "ArrowRight" ? 1 : -1);
    else if (k === "+" || k === "=" || k === ";") changeFps(1);
    else if (k === "-" || k === "_") changeFps(-1);
    else if (k === "r" || k === "R") setDialog(true);
    else if (k === "c" || k === "C") cycleCodes();
    else if (k === "f" || k === "F") toggleFullscreen();
    else return;
    e.preventDefault();
  };
  useEffect(() => {
    const h = (e: KeyboardEvent) => keys.current(e);
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  const order = s.current.order;
  const n = ui.cells.length;
  const labels = ui.cells.map((p) => entryLabel(order[p] ?? CAROUSEL_META, plan.total, repairs, n > 1));
  const cycleSec = order.length / (fps * Math.min(codes, order.length));
  const progress = ui.next === 0 ? 1 : ui.next / order.length;

  return (
    <div className="player">
      <div ref={areaRef} className="qr-area" onDoubleClick={() => waiting && start()}>
        <canvas ref={canvasRef} className="qr-canvas" />
      </div>
      <div className="player-bar">
        <div className="player-info">
          <span className="player-label">{labels.join(" / ")}</span>
          <span>
            {fps} fps{n > 1 && ` × ${n} 個（1 つずつ切り替え）`}
            {resend ? `　再送 ${resend.length} 個` : `　${ui.cycle} 周目`}
            {`　1 周 ${cycleSec < 1 ? "1 秒未満" : cycleSec < 60 ? `${cycleSec.toFixed(0)} 秒` : `${(cycleSec / 60).toFixed(1)} 分`}`}
          </span>
          {waiting && <b className="player-state">待機中: 受信側の準備ができたら「開始」（Space）</b>}
          {paused && <b className="player-state">一時停止中（←→ で 1 回分ずつ）</b>}
          {stalled && !paused && !waiting && <b className="player-state warn">QR の作成が追いついていません</b>}
          {freeze !== null && !paused && !waiting && (
            <b className="player-state warn">
              表示が {freeze.toFixed(1)} 秒止まっていました。送信中はこのページを前面に出したままにしてください
            </b>
          )}
          <div className="player-progress" aria-hidden="true">
            <div style={{ width: `${progress * 100}%` }} />
          </div>
        </div>
        <div className="player-buttons">
          <button className="pbtn main" onClick={togglePause}>
            {waiting ? "開始" : paused ? "再開" : "一時停止"}
          </button>
          <button className="pbtn" onClick={() => changeFps(-1)} aria-label="遅く" disabled={fps <= FPS_MIN}>
            −
          </button>
          <button className="pbtn" onClick={() => changeFps(1)} aria-label="速く" disabled={fps >= FPS_MAX}>
            ＋
          </button>
          <button className="pbtn" onClick={cycleCodes} title="同時に表示する QR の数（C）">
            {codes} 個
          </button>
          <button className="pbtn" onClick={() => setDialog(true)} title="欠落番号を入力して再送（R）">
            再送
          </button>
          <button className="pbtn" onClick={toggleFullscreen} title="全画面（F）">
            {fullscreen ? "全画面を解除" : "全画面"}
          </button>
          <button className="pbtn" onClick={close} title="終了（Esc）">
            閉じる
          </button>
        </div>
      </div>
      {dialog && (
        <ResendDialog
          total={plan.total}
          current={resend}
          onCancel={() => setDialog(false)}
          onAll={() => {
            setDialog(false);
            setResend(null);
            changeOrder(carouselOrder(plan.total, null, repairs));
          }}
          onResend={(seqs) => {
            setDialog(false);
            setResend(seqs);
            changeOrder(carouselOrder(plan.total, seqs));
          }}
        />
      )}
    </div>
  );
}

function ResendDialog({
  total,
  current,
  onCancel,
  onAll,
  onResend,
}: {
  total: number;
  current: number[] | null;
  onCancel: () => void;
  onAll: () => void;
  onResend: (seqs: number[]) => void;
}) {
  const [text, setText] = useState(current ? formatRanges(current) : "");
  const [error, setError] = useState<string | null>(null);
  const submit = () => {
    try {
      const seqs = [...parseRanges(text, total)].sort((a, b) => a - b);
      if (!seqs.length) return setError("番号を入力してください");
      onResend(seqs);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <div className="dialog-backdrop" onKeyDown={(e) => e.key === "Escape" && onCancel()}>
      <div className="dialog card" role="dialog" aria-modal="true" aria-label="再送">
        <h2>再送する番号</h2>
        <p className="muted">受信側に表示された欠落番号を入力してください（例: 12,57-60,99）。その番号だけを繰り返し表示します。</p>
        <textarea
          autoFocus
          rows={3}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder={`0〜${total - 1}`}
        />
        {error && <p className="banner danger">{error}</p>}
        <div className="actions">
          <button className="primary" onClick={submit}>
            再送を始める
          </button>
          {current && <button onClick={onAll}>すべて送る（通常に戻す）</button>}
          <button className="ghost" onClick={onCancel}>
            キャンセル
          </button>
        </div>
      </div>
    </div>
  );
}
