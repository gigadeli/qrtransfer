import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Assembler, type CompletionResult, type ReceivedFile, type Snapshot } from "./lib/assembler";
import { formatRanges, hex32, parseFrame } from "./lib/protocol";
import { ChunkMap } from "./components/ChunkMap";
import { useScanner } from "./useScanner";

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

function saveFile(file: ReceivedFile): void {
  const url = URL.createObjectURL(new Blob([file.data as BlobPart], { type: file.mime }));
  const a = document.createElement("a");
  a.href = url;
  a.download = file.fileName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

function toShareFile(file: ReceivedFile): File {
  return new File([file.data as BlobPart], file.fileName, { type: file.mime });
}

function canShare(file: ReceivedFile): boolean {
  try {
    return !!navigator.canShare?.({ files: [toShareFile(file)] });
  } catch {
    return false;
  }
}

export default function App() {
  const assembler = useMemo(() => new Assembler(), []);
  const [snap, setSnap] = useState<Snapshot>(() => assembler.snapshot());
  const [version, setVersion] = useState(0);
  const [enhance, setEnhanceState] = useState(true);
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

  const missing = useMemo(() => {
    if (!snap.sessionId || !snap.total || snap.received >= snap.total) return { full: "", display: "" };
    const m = assembler.missing();
    return { full: formatRanges(m), display: formatRanges(m, MISSING_DISPLAY_ITEMS) };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assembler, snap.sessionId, snap.received, snap.total]);

  const copyMissing = async () => {
    try {
      await navigator.clipboard.writeText(missing.full);
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

  const result = snap.result;
  const frac = snap.total ? snap.received / snap.total : snap.meta ? 1 : 0;
  const overlay = scanner.overlay && performance.now() - scanner.overlay.time < OVERLAY_TTL_MS ? scanner.overlay : null;

  return (
    <div className="app">
      <header className="top">
        <h1>QRTransfer</h1>
        <span className="muted">受信</span>
      </header>

      <section className={`camera card ${scanner.running ? "on" : ""}`}>
        <div className="viewport">
          <video ref={scanner.videoRef} playsInline muted />
          {overlay && (
            <svg className="overlay" viewBox={`0 0 ${overlay.width} ${overlay.height}`} preserveAspectRatio="xMidYMid meet">
              {overlay.boxes.map((b, i) => (
                <polygon key={i} points={b.points.map((p) => p.join(",")).join(" ")} className={b.ok ? "ok" : "bad"} />
              ))}
            </svg>
          )}
          {!scanner.running && (
            <div className="placeholder">
              <button className="primary big" onClick={() => void scanner.start()}>
                カメラを起動
              </button>
              <p className="muted">パソコンの QRTransfer で送信を始め、画面の QR にカメラを向けてください</p>
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
            <label className="switch">
              <input type="checkbox" checked={enhance} onChange={(e) => toggleEnhance(e.target.checked)} />
              <span>画像補正</span>
            </label>
            <button className="ghost" onClick={scanner.stop}>
              停止
            </button>
          </div>
        )}
        {scanner.running && scanner.stats.videoWidth > 0 && (
          <p className="stats muted">
            {scanner.stats.videoWidth}×{scanner.stats.videoHeight}　読み取り {scanner.stats.decodesPerSec.toFixed(0)} 回/秒
            {scanner.stats.rescued > 0 && `　補正で読めた ${scanner.stats.rescued} 枚`}
          </p>
        )}
      </section>

      {snap.conflictSessionId !== null && (
        <div className="banner warning row">
          <span>⚠ 別の転送を検出しました（session {hex32(snap.conflictSessionId)}）</span>
          <button onClick={() => assembler.switchToConflict()}>切り替え</button>
        </div>
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
              <div className="actions">
                {canShare(result.file) && (
                  <button className="primary" onClick={() => void navigator.share({ files: [toShareFile(result.file!)] }).catch(() => undefined)}>
                    共有・保存
                  </button>
                )}
                <button className={canShare(result.file) ? "" : "primary"} onClick={() => saveFile(result.file!)}>
                  ダウンロード
                </button>
                <button className="ghost" onClick={() => assembler.reset()}>
                  次の受信へ
                </button>
              </div>
            </>
          ) : (
            <>
              <h2>受信失敗</h2>
              <p className="name">{result.meta.name}</p>
              <p>{result.message}</p>
              <p className="muted">送信をやり直してください。</p>
              <div className="actions">
                <button className="primary" onClick={() => assembler.reset()}>
                  次の受信へ
                </button>
              </div>
            </>
          )}
        </section>
      )}

      {!result && (
        <section className="card progress">
          <h2>進捗</h2>
          <dl>
            <dt>ファイル</dt>
            <dd>
              {snap.sessionId === null
                ? "転送待ち"
                : snap.meta
                  ? `${snap.meta.name}　${humanSize(snap.meta.raw_size)}`
                  : "メタ情報待ち"}
            </dd>
            <dt>受信率</dt>
            <dd>
              <div className="bar">
                <div style={{ width: `${(frac * 100).toFixed(1)}%` }} />
              </div>
              <span className="pct">{snap.finishing ? "照合中…" : `${Math.floor(frac * 100)}%`}</span>
            </dd>
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
              </>
            )}
          </dl>
          {snap.total > 0 && <ChunkMap bitmap={snap.bitmap} total={snap.total} version={version} />}
        </section>
      )}

      {!result && missing.display && (
        <section className="card missing">
          <h2>欠落番号</h2>
          <p className="muted">送信側で R キーを押して入力すると再送モードになります</p>
          <pre>{missing.display}</pre>
          <div className="actions">
            <button onClick={() => void copyMissing()}>{copied ? "コピーしました" : "コピー"}</button>
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
