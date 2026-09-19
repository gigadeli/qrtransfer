import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Assembler, type CompletionResult, type ReceivedFile, type Snapshot } from "./lib/assembler";
import { hex32, parseFrame } from "./lib/protocol";
import { ChunkMap } from "./components/ChunkMap";
import { Preview } from "./components/Preview";
import { type CameraMode, useScanner } from "./useScanner";

const OVERLAY_TTL_MS = 400;
const MISSING_DISPLAY_ITEMS = 200;

export function humanSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

export function humanTime(sec: number): string {
  const s = Math.round(sec);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分 ${s % 60} 秒`;
  return `${Math.floor(m / 60)} 時間 ${m % 60} 分`;
}

/** 取りこぼしがこの割合を超えたら、送信側の設定を見直すよう知らせる */
const LOSS_WARN = 0.3;

const HUD_KEY = "qrtransfer.hud";

const MODES: { value: CameraMode; label: string; detail: string }[] = [
  { value: "quality", label: "高解像度", detail: "1920×1080" },
  { value: "speed", label: "高速", detail: "1280×720・60fps" },
];

/** 「進捗を映像に表示」の設定（この端末のブラウザに覚える。使えない環境では既定の ON） */
function loadHud(): boolean {
  try {
    return localStorage.getItem(HUD_KEY) !== "0";
  } catch {
    return true;
  }
}

function saveHud(on: boolean): void {
  try {
    localStorage.setItem(HUD_KEY, on ? "1" : "0");
  } catch {
    /* 保存できなくても動作には関係ない */
  }
}

function saveFile(file: ReceivedFile): void {
  // 種類は常に octet-stream にする。download 属性を無視するブラウザ（アプリ内ブラウザなど）でも、
  // 受信した HTML・SVG がこのサイトのページとして開かれ、中のスクリプトが動くことがないように
  const url = URL.createObjectURL(new Blob([file.data as BlobPart], { type: "application/octet-stream" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = file.fileName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

/** 共有メニュー用のファイル。File を作るとデータがコピーされるので、受信結果ごとに 1 回だけ作る。 */
function makeShareFile(file: ReceivedFile | undefined): File | null {
  if (!file || !navigator.canShare) return null;
  try {
    const f = new File([file.data as BlobPart], file.fileName, { type: file.mime });
    return navigator.canShare({ files: [f] }) ? f : null;
  } catch {
    return null;
  }
}

export default function App() {
  const assembler = useMemo(() => new Assembler(), []);
  const [snap, setSnap] = useState<Snapshot>(() => assembler.snapshot());
  const [version, setVersion] = useState(0);
  const [enhance, setEnhanceState] = useState(true);
  // カメラの映像に進捗を重ねて表示する（スクロールせずに確認でき、スマホを動かさずに済む）
  const [hud, setHud] = useState(loadHud);
  const [copied, setCopied] = useState(false);
  const dirty = useRef(true);

  const onFrame = useCallback(
    (bytes: Uint8Array) => {
      const frame = parseFrame(bytes);
      if (!frame) return false;
      return assembler.feed(frame) !== "invalid";
    },
    [assembler],
  );
  const scanner = useScanner(onFrame);

  useEffect(() => {
    assembler.onChange = () => {
      dirty.current = true;
    };
    assembler.onComplete = (r: CompletionResult) => {
      dirty.current = true;
      if (r.ok) navigator.vibrate?.(200);
    };
    const timer = setInterval(() => {
      // 受信の状況は 4 回/秒だけ画面に反映する（フレームごとに描き直すと重いため）
      const s = assembler.snapshot();
      setSnap(s);
      if (dirty.current) {
        dirty.current = false;
        setVersion((v) => v + 1);
      }
    }, 250);
    return () => clearInterval(timer);
  }, [assembler]);

  const missingDisplay = useMemo(() => {
    if (snap.sessionId === null || !snap.total || snap.received >= snap.total) return "";
    return assembler.missingText(MISSING_DISPLAY_ITEMS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assembler, snap.sessionId, snap.received, snap.total]);

  const result = snap.result;
  const done = snap.finishing || !!result;

  // 全チャンクがそろった（照合を始めた）時点でカメラを止める。読み取りの処理も止まり、照合が速く終わる
  const stopScanner = scanner.stop;
  useEffect(() => {
    if (done && scanner.running) stopScanner();
  }, [done, scanner.running, stopScanner]);

  /** 結果を閉じて次の受信を待つ。カメラは自動で止めたので、もう一度起動する */
  const next = () => {
    assembler.reset();
    void scanner.start();
  };

  const shareFile = useMemo(() => makeShareFile(result?.file), [result]);

  const copyMissing = async () => {
    try {
      await navigator.clipboard.writeText(assembler.missingText());
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* クリップボードが使えない場合は、表示を長押しでコピーしてもらう */
    }
  };

  const toggleEnhance = (on: boolean) => {
    setEnhanceState(on);
    scanner.setEnhance(on);
  };

  const switchSession = () => {
    if (snap.received > 0 && !confirm("今の受信を中断して、別の転送に切り替えますか？（受信済みのデータは破棄されます）")) return;
    assembler.switchToConflict();
  };

  // 修復用の式も進み具合に含める（式がそろった時点でまとめて解けるため、チャンク数だけだと止まって見える）
  const frac = snap.total ? (snap.received + snap.pending) / snap.total : snap.meta ? 1 : 0;
  // 修復用 QR から集める式: 欠けたチャンクの数だけ集まると、欠けた分がまとめて復元される
  const lacking = snap.total - snap.received;
  const repairing = snap.repair && lacking > 0 && !snap.finishing;
  const lossHigh = snap.loss !== null && snap.loss >= LOSS_WARN;
  const overlay = scanner.running && scanner.overlay && performance.now() - scanner.overlay.time < OVERLAY_TTL_MS ? scanner.overlay : null;

  return (
    <div className={`app ${scanner.running ? "scanning" : ""}`}>
      <header className="top">
        <span className="logo" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M5 14v5h14v-5M12 4v11M8 11l4 4 4-4" />
          </svg>
        </span>
        <h1>QRTransfer</h1>
        <span className="muted">受信</span>
        <span className="build muted">{__BUILD_INFO__}</span>
      </header>

      <section className={`camera card ${scanner.running ? "on" : ""} ${done && !scanner.running ? "done" : ""}`}>
        <div className="viewport">
          <video ref={scanner.videoRef} playsInline muted />
          {overlay && (
            <svg className="overlay" viewBox={`0 0 ${overlay.width} ${overlay.height}`} preserveAspectRatio="xMidYMid meet">
              {overlay.boxes.map((b, i) => (
                <polygon key={i} points={b.points.map((p) => p.join(",")).join(" ")} className={b.ok ? "ok" : "bad"} />
              ))}
            </svg>
          )}
          {scanner.running && hud && !result && (
            <div className="hud" aria-live="off">
              {snap.sessionId === null ? (
                <span className="hud-line">転送待ち</span>
              ) : (
                <>
                  <div className="hud-line">
                    <b>{snap.finishing ? "照合中…" : `${(frac * 100).toFixed(frac < 1 ? 1 : 0)}%`}</b>
                    <span>
                      {repairing ? `修復 ${snap.pending} / ${lacking}` : `${snap.received} / ${snap.total}`}
                    </span>
                    {snap.etaSec !== null && <span>残り {humanTime(snap.etaSec)}</span>}
                    {snap.loss !== null && (
                      <span className={lossHigh ? "hud-warn" : undefined}>取りこぼし {Math.round(snap.loss * 100)}%</span>
                    )}
                  </div>
                  <div className="bar hud-bar">
                    <div style={{ width: `${((snap.total ? snap.received / snap.total : frac) * 100).toFixed(2)}%` }} />
                    {snap.pending > 0 && (
                      <div className="pending" style={{ width: `${((snap.pending / snap.total) * 100).toFixed(2)}%` }} />
                    )}
                  </div>
                </>
              )}
            </div>
          )}
          {!scanner.running && (
            <div className="placeholder">
              {done ? (
                <>
                  <p className="done-note">{result ? "受信が終わったので、カメラを止めました" : "すべて受信しました。照合しています…"}</p>
                  {result && <button onClick={next}>次の受信を始める</button>}
                </>
              ) : (
                <>
                  <button className="primary big" onClick={() => void scanner.start()}>
                    カメラを起動
                  </button>
                  <p className="muted">パソコンの QRTransfer で送信を始め、画面の QR にカメラを向けてください</p>
                </>
              )}
            </div>
          )}
        </div>
        {scanner.error && <p className="banner danger">{scanner.error}</p>}
        {scanner.running && (
          <div className="controls">
            {scanner.devices.length > 1 && (
              <select value={scanner.deviceId ?? ""} onChange={(e) => scanner.switchDevice(e.target.value)} aria-label="カメラ">
                {scanner.devices.map((d) => (
                  <option key={d.deviceId} value={d.deviceId}>
                    {d.label}
                  </option>
                ))}
              </select>
            )}
            <div
              className="segmented"
              role="group"
              aria-label="カメラの撮り方"
              title="高速: 1 枚の撮影時間が短く、QR の切り替わりが混ざった画像が減ります。1 マスが粗くなるので、QR を複数並べるときは高解像度がおすすめです"
              style={{ "--count": MODES.length, "--index": MODES.findIndex((m) => m.value === scanner.mode) } as React.CSSProperties}
            >
              <span className="thumb" aria-hidden="true" />
              {MODES.map((m) => (
                <button key={m.value} aria-pressed={scanner.mode === m.value} onClick={() => scanner.setMode(m.value)}>
                  {m.label}
                  <small>{m.detail}</small>
                </button>
              ))}
            </div>
            <label className="switch">
              <input type="checkbox" checked={enhance} onChange={(e) => toggleEnhance(e.target.checked)} />
              <span>画像補正</span>
            </label>
            <label className="switch">
              <input
                type="checkbox"
                checked={hud}
                onChange={(e) => {
                  setHud(e.target.checked);
                  saveHud(e.target.checked);
                }}
              />
              <span>進捗を映像に表示</span>
            </label>
            <button className="stop" onClick={scanner.stop}>
              停止
            </button>
          </div>
        )}
        {scanner.running && scanner.stats.videoWidth > 0 && (
          <p className="stats muted">
            {scanner.stats.videoWidth}×{scanner.stats.videoHeight}
            {scanner.stats.videoFps > 0 && ` ${Math.round(scanner.stats.videoFps)}fps`}　読み取り {scanner.stats.decodesPerSec.toFixed(0)} 回/秒
            {scanner.workers > 1 && `（${scanner.workers} 並列）`}
            {scanner.stats.rescued > 0 && `　補正で読めた ${scanner.stats.rescued} 枚`}
          </p>
        )}
      </section>

      {snap.conflictSessionId !== null && (
        <div className="banner warning row">
          <span>⚠ 別の転送を検出しました（session {hex32(snap.conflictSessionId)}）</span>
          <button onClick={switchSession}>切り替え</button>
        </div>
      )}

      {snap.rejected && (
        <p className="banner warning">
          ⚠ {snap.rejected.name ? `「${snap.rejected.name}」（${humanSize(snap.rejected.size ?? 0)}）は` : "この転送は"}
          このページで受信できる大きさ（{humanSize(snap.rejected.limit)}）を超えています。パソコン版の QRTransfer で受信してください。
        </p>
      )}

      {result && (
        <section className={`card result ${result.ok ? "success" : "danger"}`}>
          {result.ok && result.file ? (
            <>
              <h2>受信完了</h2>
              <p className="name">{result.file.fileName}</p>
              <p className="muted">
                {humanSize(result.meta.raw_size)}（転送 {humanSize(result.meta.payload_size)}）　{humanTime(result.elapsedMs / 1000)}
                {result.file.fileCount !== undefined && `　${result.file.fileCount} ファイル`}
              </p>
              <p className="muted">{result.message}</p>
              {!!result.file.skipped?.length && (
                <p className="muted">スキップした項目: {result.file.skipped.map((s) => s.name).slice(0, 10).join(", ")}</p>
              )}
              {!!result.file.renamed?.length && (
                <p className="muted">
                  名前が重なるため変更した項目: {result.file.renamed.map((r) => `${r.name} → ${r.to}`).slice(0, 10).join(", ")}
                </p>
              )}
              <Preview file={result.file} />
              <div className="actions">
                {shareFile && (
                  <button className="primary" onClick={() => void navigator.share({ files: [shareFile] }).catch(() => undefined)}>
                    共有・保存
                  </button>
                )}
                <button className={shareFile ? "" : "primary"} onClick={() => saveFile(result.file!)}>
                  ダウンロード
                </button>
                <button className="ghost" onClick={next}>
                  次の受信へ
                </button>
              </div>
            </>
          ) : (
            <>
              <h2>受信失敗</h2>
              <p className="name">{result.meta.name}</p>
              <p>{result.message}</p>
              <p className="muted">パソコン側で送信を始め直してください（同じ送信の表示を続けても、この転送は受信しません）。</p>
              <div className="actions">
                <button className="primary" onClick={next}>
                  次の受信へ
                </button>
              </div>
            </>
          )}
        </section>
      )}

      {!result && (
        <section className="card progress">
          <div className="headline">
            <h2>進捗</h2>
            <span className="big-pct">
              {snap.finishing ? (
                "照合中…"
              ) : (
                <>
                  {(frac * 100).toFixed(frac < 1 ? 1 : 0)}
                  <small>%</small>
                </>
              )}
            </span>
          </div>
          <div className="bar">
            <div style={{ width: `${((snap.total ? snap.received / snap.total : frac) * 100).toFixed(2)}%` }} />
            {snap.pending > 0 && (
              <div className="pending" style={{ width: `${((snap.pending / snap.total) * 100).toFixed(2)}%` }} />
            )}
          </div>
          <dl>
            <dt>ファイル</dt>
            <dd>
              {snap.sessionId === null
                ? "転送待ち"
                : snap.meta
                  ? `${snap.meta.name}　${humanSize(snap.meta.raw_size)}`
                  : "メタ情報待ち"}
            </dd>
            {repairing && (
              <>
                <dt>修復</dt>
                <dd className="repairing">
                  <div className="bar small">
                    <div className="pending" style={{ width: `${((snap.pending / lacking) * 100).toFixed(1)}%` }} />
                  </div>
                  <span className="count">
                    {snap.pending} / {lacking}
                  </span>
                </dd>
                <dd className="note muted">
                  修復用 QR を読むたびに増え、欠けた {lacking} チャンク分がそろうとまとめて復元されます
                </dd>
              </>
            )}
            {snap.sessionId !== null && (
              <>
                <dt>チャンク</dt>
                <dd>
                  {snap.received} / {snap.total}
                </dd>
                <dt>受信速度</dt>
                <dd>
                  {snap.ratePerSec.toFixed(1)} チャンク/秒　{(snap.bytesPerSec / 1024).toFixed(1)} KB/秒
                </dd>
                <dt>残り時間</dt>
                <dd>{snap.etaSec !== null ? humanTime(snap.etaSec) : "-"}</dd>
                {snap.loss !== null && (
                  <>
                    <dt>取りこぼし</dt>
                    <dd className={lossHigh ? "warn" : undefined}>
                      約 {Math.round(snap.loss * 100)}%<span className="muted">（直近 5 秒の推定）</span>
                    </dd>
                  </>
                )}
              </>
            )}
          </dl>
          {lossHigh && (
            <p className="banner warning">
              読み取りが表示に追いついていません。送信側の<b>表示速度（fps）</b>か<b>同時に表示する QR の数</b>を下げてください。
              スマホを固定し、本体が熱くなっていないか（熱くなると処理が遅くなります）も確認してください。
            </p>
          )}
          {snap.total > 0 && <ChunkMap bitmap={snap.bitmap} total={snap.total} version={version} />}
        </section>
      )}

      {!result && missingDisplay && (
        <section className="card missing">
          {/* 修復用 QR を受け取っている転送では、読み続けるだけで完了するので欠落番号（再送用）を出さない */}
          {!snap.repair && (
            <>
              <h2>欠落番号</h2>
              <p className="muted">送信側で R キーを押して入力すると再送モードになります</p>
              <pre>{missingDisplay}</pre>
            </>
          )}
          <div className="actions">
            {!snap.repair && <button onClick={() => void copyMissing()}>{copied ? "コピーしました" : "コピー"}</button>}
            <button
              className="ghost"
              onClick={() => {
                if (confirm("受信中のデータを破棄しますか？")) assembler.discardCurrent();
              }}
            >
              この受信を破棄
            </button>
          </div>
        </section>
      )}
    </div>
  );
}
