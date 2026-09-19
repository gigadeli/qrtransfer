import { useEffect, useMemo, useRef, useState } from "react";
import { humanSize, humanTime } from "../lib/format";
import { ModeNav } from "../components/ModeNav";
import { VersionError, chooseVersion, framesPerCycle, modulesWithBorder } from "../lib/carousel";
import { CHUNK_SIZE_MAX, CHUNK_SIZE_MIN } from "../lib/meta";
import { MAX_RAW_SIZE, type SendItem, itemsSize } from "../lib/pack";
import { type Ecc, capacity } from "../lib/qrcode";
import { RATIO_MAX, RATIO_MIN, repairCount } from "../lib/repair";
import { FrameClient } from "./client";
import { canPickDirectory, fromDataTransfer, fromDirectoryInput, fromFileList } from "./files";
import type { PlanInfo } from "./frameWorker";
import { Player } from "./Player";
import { CODES_MAX, FPS_MAX, FPS_MIN, type Method, type SendSettings, loadSettings, normalize, recommendedEcc, saveSettings } from "./settings";

const ECCS: { value: Ecc; label: string }[] = [
  { value: "l", label: "L" },
  { value: "m", label: "M" },
  { value: "q", label: "Q" },
  { value: "h", label: "H" },
];

function Segmented<T extends string | number>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: string; detail?: string }[];
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div
      className="segmented"
      role="group"
      aria-label={label}
      style={{ "--count": options.length, "--index": Math.max(0, options.findIndex((o) => o.value === value)) } as React.CSSProperties}
    >
      <span className="thumb" aria-hidden="true" />
      {options.map((o) => (
        <button key={String(o.value)} aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
          {o.detail && <small>{o.detail}</small>}
        </button>
      ))}
    </div>
  );
}

/**
 * 数の入力欄。打っている途中（"1" → "15" → "1500"）は丸めずにそのまま見せ、範囲内の数になったら反映する。
 * 範囲外のまま欄を離れたら（または Enter）、範囲内に丸める。
 */
function NumberField({
  id,
  value,
  min,
  max,
  step,
  onChange,
}: {
  id: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
}) {
  const [text, setText] = useState(String(value));
  useEffect(() => setText(String(value)), [value]);
  const commit = () => {
    const n = Number(text);
    if (!text.trim() || !Number.isFinite(n)) return setText(String(value));
    const v = Math.min(max, Math.max(min, Math.round(n)));
    onChange(v);
    setText(String(v));
  };
  return (
    <input
      id={id}
      type="number"
      min={min}
      max={max}
      step={step}
      value={text}
      onChange={(e) => {
        const t = e.target.value;
        setText(t);
        const n = Number(t);
        if (t.trim() && Number.isInteger(n) && n >= min && n <= max) onChange(n);
      }}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && commit()}
    />
  );
}

function itemLabel(it: SendItem): string {
  if (it.kind === "file") return humanSize(it.data.size);
  const { size, count } = itemsSize([it]);
  return `フォルダ・${count} ファイル・${humanSize(size)}`;
}

export default function SendApp() {
  const client = useMemo(() => new FrameClient(), []);
  const [items, setItems] = useState<SendItem[]>([]);
  const [settings, setSettings] = useState<SendSettings>(loadSettings);
  const [plan, setPlan] = useState<PlanInfo | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const dirInput = useRef<HTMLInputElement>(null);
  const planned = useRef<{ items: SendItem[]; chunkSize: number } | null>(null);
  const request = useRef(0);

  useEffect(() => () => client.terminate(), [client]);

  const update = (patch: Partial<SendSettings>) => {
    setSettings((prev) => {
      const next = normalize({ ...prev, ...patch });
      saveSettings(next);
      return next;
    });
  };

  // 送るものかチャンクサイズが変わったら、送信データを作り直す（梱包・圧縮は Worker で）
  useEffect(() => {
    if (!items.length) {
      setPlan(null);
      setError(null);
      planned.current = null;
      return;
    }
    const id = ++request.current;
    const same = planned.current?.items === items;
    const timer = window.setTimeout(async () => {
      setPreparing(true);
      setError(null);
      try {
        const full = () => client.request<"planned">({ type: "plan", items, chunkSize: settings.chunkSize });
        // チャンクサイズだけが変わったなら、梱包・圧縮をやり直さない（Worker が作り直されていたら最初から）
        const res = same ? await client.request<"planned">({ type: "chunk", chunkSize: settings.chunkSize }).catch(full) : await full();
        if (id !== request.current) return;
        planned.current = { items, chunkSize: settings.chunkSize };
        setPlan(res.info);
      } catch (e) {
        if (id !== request.current) return;
        setPlan(null);
        planned.current = null;
        setError((e as Error).message);
      } finally {
        if (id === request.current) setPreparing(false);
      }
    }, 250);
    return () => window.clearTimeout(timer);
  }, [client, items, settings.chunkSize]);

  const add = (more: SendItem[]) => {
    if (more.length) setItems((prev) => [...prev, ...more]);
  };

  const total = itemsSize(items);
  const repairs = plan && settings.method === "repair" ? repairCount(plan.total, settings.repairRatio) : 0;
  let version: number | null = null;
  let versionError: string | null = null;
  if (plan) {
    try {
      version = chooseVersion(plan.maxDataFrameSize, plan.minMetaFrameSize, settings.ecc);
    } catch (e) {
      if (!(e instanceof VersionError)) throw e;
      versionError = e.message;
    }
  }
  const frames = plan ? framesPerCycle(plan.total, repairs) : 0;
  const cycleSec = frames / (settings.fps * settings.codes);
  const overLimit = total.size > MAX_RAW_SIZE;
  // 表示中の見積もりが、今の送るもの・チャンクサイズで作ったものか（変えた直後は作り直しを待つ。
  // 古い計画のまま始めると、表示順と Worker の中の計画が食い違う）
  const ready = !!plan && planned.current?.items === items && planned.current.chunkSize === settings.chunkSize;

  const start = () => {
    if (!ready) return;
    // 全画面は待たずに頼むだけ（応答しない環境もある。できない環境ではページいっぱいに表示する）
    document.documentElement.requestFullscreen?.().catch(() => undefined);
    setPlaying(true);
  };

  if (playing && plan && version) {
    return (
      <Player
        client={client}
        plan={plan}
        version={version}
        ecc={settings.ecc}
        repairs={repairs}
        fps={settings.fps}
        codes={settings.codes}
        onClose={(st) => {
          update({ fps: st.fps, codes: st.codes });
          setPlaying(false);
        }}
      />
    );
  }

  return (
    <div className="app">
      <header className="top">
        <ModeNav current="send" />
        <span className="build muted">{__BUILD_INFO__}</span>
      </header>

      <section
        className={`card pick ${dragging ? "dragging" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          void fromDataTransfer(e.dataTransfer).then(add);
        }}
      >
        <h2>送るもの</h2>
        {items.length === 0 ? (
          <div className="dropzone">
            <p>ここにファイルやフォルダをドロップ</p>
            <p className="muted">ファイルはこの端末の中だけで処理し、どこにも送信しません</p>
          </div>
        ) : (
          <ul className="items">
            {items.map((it, i) => (
              <li key={i}>
                <span className="item-icon" aria-hidden="true">
                  {it.kind === "dir" ? "📁" : "📄"}
                </span>
                <span className="item-name">{it.name}</span>
                <span className="muted">{itemLabel(it)}</span>
                <button className="ghost small" aria-label={`${it.name} を外す`} onClick={() => setItems(items.filter((_, k) => k !== i))}>
                  ✕
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="actions">
          <button onClick={() => fileInput.current?.click()}>ファイルを選ぶ</button>
          {canPickDirectory() && <button onClick={() => dirInput.current?.click()}>フォルダを選ぶ</button>}
          {items.length > 0 && (
            <button className="ghost" onClick={() => setItems([])}>
              すべて外す
            </button>
          )}
        </div>
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            add(fromFileList(e.target.files ?? []));
            e.target.value = "";
          }}
        />
        <input
          ref={dirInput}
          type="file"
          hidden
          {...({ webkitdirectory: "" } as Record<string, string>)}
          onChange={(e) => {
            add(fromDirectoryInput(e.target.files ?? []));
            e.target.value = "";
          }}
        />
        {overLimit && <p className="banner danger">合計 {humanSize(total.size)} は、送信できる大きさ（100 MB）を超えています。</p>}
      </section>

      <section className="card settings">
        <h2>設定</h2>
        <div className="field">
          <span className="field-label">送信方式</span>
          <Segmented<Method>
            label="送信方式"
            value={settings.method}
            onChange={(m) => update({ method: m, ecc: recommendedEcc(m) })}
            options={[
              { value: "repair", label: "修復用 QR あり", detail: "取りこぼしても完了" },
              { value: "classic", label: "従来方式", detail: "欠落番号を再送" },
            ]}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="fps">
            表示速度 <b>{settings.fps} fps</b>
          </label>
          <input id="fps" type="range" min={FPS_MIN} max={FPS_MAX} value={settings.fps} onChange={(e) => update({ fps: Number(e.target.value) })} />
          <p className="hint muted">受信側の読み取りが追いつく速さにします。スマホで受信するなら 6〜10 fps から試してください。</p>
        </div>
        <div className="field">
          <span className="field-label">同時に表示する QR</span>
          <Segmented<number>
            label="同時に表示する QR の数"
            value={settings.codes}
            onChange={(c) => update({ codes: c })}
            options={Array.from({ length: CODES_MAX }, (_, i) => ({ value: i + 1, label: `${i + 1} 個` }))}
          />
          <p className="hint muted">並べるほど速く送れますが、1 つが小さくなります。受信側はカメラを「高解像度」にしてください。</p>
        </div>
        <div className="field">
          <span className="field-label">誤り訂正</span>
          <Segmented<Ecc> label="誤り訂正" value={settings.ecc} onChange={(e) => update({ ecc: e })} options={ECCS} />
          <p className="hint muted">
            高いほど汚れや映り込みに強くなりますが、1 枚に入る量が減ります。{settings.method === "repair" ? "修復用 QR を使うなら L がおすすめです。" : "従来方式なら M がおすすめです。"}
          </p>
        </div>
        <div className="field row">
          <label className="field-label" htmlFor="chunk">
            チャンクサイズ
          </label>
          <NumberField
            id="chunk"
            min={CHUNK_SIZE_MIN}
            max={CHUNK_SIZE_MAX}
            step={100}
            value={settings.chunkSize}
            onChange={(v) => update({ chunkSize: v })}
          />
          <span className="muted">バイト（{CHUNK_SIZE_MIN}〜{CHUNK_SIZE_MAX}）</span>
        </div>
        {settings.method === "repair" && (
          <div className="field row">
            <label className="field-label" htmlFor="ratio">
              修復用 QR の割合
            </label>
            <NumberField
              id="ratio"
              min={RATIO_MIN}
              max={RATIO_MAX}
              step={10}
              value={settings.repairRatio}
              onChange={(v) => update({ repairRatio: v })}
            />
            <span className="muted">%（{RATIO_MIN}〜{RATIO_MAX}）</span>
          </div>
        )}
      </section>

      {items.length > 0 && !overLimit && (
        <section className="card estimate">
          <h2>見積もり</h2>
          {(preparing || (!ready && !error)) && <p className="muted">準備中…（ファイルの読み込みと圧縮）</p>}
          {error && <p className="banner danger">{error}</p>}
          {plan && ready && !preparing && (
            <>
              <dl>
                <dt>名前</dt>
                <dd>
                  {plan.meta.name}
                  {plan.meta.mode === "bundle" && <span className="muted">（{plan.fileCount} ファイルをまとめて送信）</span>}
                </dd>
                <dt>大きさ</dt>
                <dd>
                  {humanSize(plan.meta.raw_size)}
                  {plan.meta.compression === "zlib"
                    ? ` → 圧縮して ${humanSize(plan.meta.payload_size)}（${Math.round((plan.meta.payload_size / Math.max(1, plan.meta.raw_size)) * 100)}%）`
                    : "（圧縮しても小さくならないため、そのまま）"}
                </dd>
                <dt>チャンク</dt>
                <dd>{plan.total} 個</dd>
                {repairs > 0 && (
                  <>
                    <dt>修復用 QR</dt>
                    <dd>{repairs} 枚</dd>
                  </>
                )}
                {version && (
                  <>
                    <dt>QR</dt>
                    <dd>
                      バージョン {version}（{modulesWithBorder(version)}×{modulesWithBorder(version)} マス・1 枚 {capacity(version, settings.ecc)} バイトまで）
                    </dd>
                    <dt>1 周</dt>
                    <dd>
                      {frames} 枚・約 {humanTime(cycleSec)}
                    </dd>
                  </>
                )}
              </dl>
              {versionError && <p className="banner danger">{versionError}</p>}
              {plan.meta.raw_size > 20 * 1024 * 1024 && (
                <p className="banner warning">大きいファイルは時間がかかります。フォルダは必要なものだけにする、あらかじめ圧縮するなどを検討してください。</p>
              )}
            </>
          )}
          <div className="actions">
            <button className="primary big" disabled={!ready || !version || preparing} onClick={start}>
              送信を始める
            </button>
          </div>
          <p className="hint muted">
            受信側でカメラを起動してから始めてください。操作: Space 開始・一時停止／←→ 1 枚ずつ／+ − 速さ／C QR の数／R 再送／F 全画面／Esc 終了
          </p>
        </section>
      )}
    </div>
  );
}
