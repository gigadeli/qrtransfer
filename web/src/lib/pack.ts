/**
 * 送信データの作成: 梱包（file / bundle）、圧縮、ハッシュ、チャンク分割、フレームの生成。
 * Python 版 src/qrtransfer/packer.py と同じ形式のデータを作る（パソコン版・Web 版のどちらの受信でも受け取れる）。
 *
 * 違いは圧縮だけ: ブラウザには xz（lzma）で圧縮する軽い手段がないため、zlib（レベル 9）だけを試す。
 */
import { zlibSync } from "fflate";
import { sha256Hex } from "./codec";
import { type Compression, type Meta, type Mode, META_NAME_MIN_BYTES, metaPayload, totalChunks, truncateName } from "./meta";
import { OVERHEAD, TYPE_DATA, TYPE_META, TYPE_REPAIR, buildFrame } from "./protocol";
import { encodeRepair } from "./repair";

/** これを超える大きさは送らない（Web 版の受信の上限と同じ。QR での転送では、これ以上は現実的な時間で終わらない） */
export const MAX_RAW_SIZE = 100 * 1024 * 1024;
/** 圧縮してもこの割合を下回らなければ、圧縮しない（Python 版 packer.COMPRESSION_RATIO） */
export const COMPRESSION_RATIO = 0.95;

/** 送るもの（ファイル、またはフォルダ）。mtime は秒 */
export type SendItem =
  | { kind: "file"; name: string; data: Blob; mtime: number }
  | { kind: "dir"; name: string; children: SendItem[]; mtime: number };

export interface Packed {
  name: string;
  mode: Mode;
  raw: Uint8Array;
  mtime: number | null;
  fileCount: number;
}

export class PackError extends Error {}

/** 送るものの合計の大きさとファイル数 */
export function itemsSize(items: SendItem[]): { size: number; count: number } {
  let size = 0;
  let count = 0;
  const walk = (it: SendItem) => {
    if (it.kind === "file") {
      size += it.data.size;
      count++;
    } else it.children.forEach(walk);
  };
  items.forEach(walk);
  return { size, count };
}

// ---------------------------------------------------------------------------
// tar（PAX 形式。Python の tarfile.PAX_FORMAT と同じく、ASCII でない名前・長い名前は PAX の path で持つ）
// ---------------------------------------------------------------------------

interface TarEntry {
  path: string;
  dir: boolean;
  mtime: number;
  data?: Blob;
}

const utf8 = new TextEncoder();

function dedupe(name: string, used: Set<string>): string {
  if (!used.has(name.toLowerCase())) {
    used.add(name.toLowerCase());
    return name;
  }
  const dot = name.lastIndexOf(".");
  const [stem, ext] = dot > 0 ? [name.slice(0, dot), name.slice(dot)] : [name, ""];
  for (let i = 1; ; i++) {
    const cand = `${stem} (${i})${ext}`;
    if (!used.has(cand.toLowerCase())) {
      used.add(cand.toLowerCase());
      return cand;
    }
  }
}

const byName = (a: SendItem, b: SendItem) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);

/** フォルダの中身を並べる（Python 版 packer._iter_tree と同じく、フォルダ自身 → ファイル → 下のフォルダの順） */
function walkTree(dir: SendItem & { kind: "dir" }, base: string, out: TarEntry[]): void {
  if (base) out.push({ path: base, dir: true, mtime: dir.mtime });
  const children = [...dir.children].sort(byName);
  for (const c of children) {
    if (c.kind === "file") out.push({ path: base ? `${base}/${c.name}` : c.name, dir: false, mtime: c.mtime, data: c.data });
  }
  for (const c of children) if (c.kind === "dir") walkTree(c, base ? `${base}/${c.name}` : c.name, out);
}

function writeString(h: Uint8Array, off: number, len: number, s: string): void {
  h.set(utf8.encode(s).subarray(0, len), off);
}

function writeOctal(h: Uint8Array, off: number, len: number, value: number): void {
  writeString(h, off, len - 1, value.toString(8).padStart(len - 1, "0"));
}

function header(name: string, size: number, mtime: number, type: string, mode: number): Uint8Array {
  const h = new Uint8Array(512);
  writeString(h, 0, 100, name);
  writeOctal(h, 100, 8, mode);
  writeOctal(h, 108, 8, 0);
  writeOctal(h, 116, 8, 0);
  writeOctal(h, 124, 12, size);
  writeOctal(h, 136, 12, Math.max(0, Math.floor(mtime)));
  h.fill(0x20, 148, 156); // チェックサムを計算する間は空白
  writeString(h, 156, 1, type);
  writeString(h, 257, 6, "ustar\0");
  writeString(h, 263, 2, "00");
  let sum = 0;
  for (const b of h) sum += b;
  writeString(h, 148, 8, `${sum.toString(8).padStart(6, "0")}\0 `);
  return h;
}

/** PAX の記録 "<長さ> path=<名前>\n"（長さは記録全体のバイト数で、自分自身の桁数を含む） */
function paxRecord(key: string, value: string): Uint8Array {
  const body = utf8.encode(` ${key}=${value}\n`);
  let len = body.length + 1;
  while (String(len).length + body.length !== len) len = String(len).length + body.length;
  return utf8.encode(`${len} ${key}=${value}\n`);
}

const pad512 = (n: number) => Math.ceil(n / 512) * 512;

async function makeTar(entries: TarEntry[]): Promise<Uint8Array> {
  const parts: { head: Uint8Array[]; data?: Blob; size: number }[] = [];
  let total = 1024; // 末尾の空ブロック 2 つ
  for (const e of entries) {
    const name = e.dir ? `${e.path}/` : e.path;
    const size = e.dir ? 0 : e.data!.size;
    const head: Uint8Array[] = [];
    // ASCII 以外を含む名前・100 バイトを超える名前は PAX の path で持つ（ustar の name には読める範囲で入れておく）
    if (!/^[\x20-\x7e]*$/.test(name) || utf8.encode(name).length > 100) {
      const rec = paxRecord("path", name);
      head.push(header("././@PaxHeader", rec.length, e.mtime, "x", 0o644), rec, new Uint8Array(pad512(rec.length) - rec.length));
    }
    const fallback = name.replace(/[^\x20-\x7e]/g, "_").slice(-100);
    head.push(header(fallback, size, e.mtime, e.dir ? "5" : "0", e.dir ? 0o755 : 0o644));
    parts.push({ head, data: e.data, size });
    total += head.reduce((s, b) => s + b.length, 0) + pad512(size);
  }
  if (total > MAX_RAW_SIZE + 1024 * 1024 * 16) throw new PackError("送信できる大きさを超えています");
  const out = new Uint8Array(total);
  let off = 0;
  for (const p of parts) {
    for (const b of p.head) {
      out.set(b, off);
      off += b.length;
    }
    if (p.data) out.set(new Uint8Array(await p.data.arrayBuffer()), off);
    off += pad512(p.size);
  }
  return out;
}

function bundleName(now: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `bundle_${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}_${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}`;
}

/**
 * 送るものを梱包する（Python 版 packer.pack_paths と同じ決まり）。
 * - 1 つのファイル → mode=file（そのまま）
 * - 1 つのフォルダ → mode=bundle（名前はフォルダ名。tar の中はフォルダの中身からの相対パス）
 * - 複数 → mode=bundle（名前は bundle_YYYYMMDD_HHMMSS。tar の中はそれぞれの名前から）
 */
export async function packItems(items: SendItem[], now = new Date()): Promise<Packed> {
  if (!items.length) throw new PackError("送信するファイルが選ばれていません");
  const { size, count } = itemsSize(items);
  if (size > MAX_RAW_SIZE) throw new PackError(`合計 ${Math.ceil(size / 1024 / 1024)} MB は、送信できる大きさ（100 MB）を超えています`);
  if (items.length === 1 && items[0].kind === "file") {
    const f = items[0];
    return { name: f.name, mode: "file", raw: new Uint8Array(await f.data.arrayBuffer()), mtime: Math.floor(f.mtime), fileCount: 1 };
  }
  const entries: TarEntry[] = [];
  let name: string;
  if (items.length === 1) {
    const root = items[0] as SendItem & { kind: "dir" };
    name = root.name || "bundle";
    walkTree(root, "", entries);
  } else {
    name = bundleName(now);
    const used = new Set<string>();
    for (const it of items) {
      const arc = dedupe(it.name || "item", used);
      if (it.kind === "dir") walkTree(it, arc, entries);
      else entries.push({ path: arc, dir: false, mtime: it.mtime, data: it.data });
    }
  }
  const raw = await makeTar(entries);
  // まとめると tar の見出しの分だけ大きくなる（受信側は raw_size で上限を判断する）
  if (raw.length > MAX_RAW_SIZE) {
    throw new PackError(`まとめると ${(raw.length / 1024 / 1024).toFixed(1)} MB になり、送信できる大きさ（100 MB）を超えます`);
  }
  return { name, mode: "bundle", raw, mtime: null, fileCount: count };
}

// ---------------------------------------------------------------------------
// 転送計画
// ---------------------------------------------------------------------------

export class Plan {
  readonly total: number;

  constructor(
    readonly sessionId: number,
    readonly meta: Meta,
    readonly payload: Uint8Array,
    readonly chunkSize: number,
  ) {
    this.total = totalChunks(payload.length, chunkSize);
  }

  chunk(i: number): Uint8Array {
    return this.payload.subarray(i * this.chunkSize, Math.min(this.payload.length, (i + 1) * this.chunkSize));
  }

  /** DATA の最大のフレームの大きさ（バイト） */
  get maxDataFrameSize(): number {
    return OVERHEAD + (this.total ? Math.min(this.chunkSize, this.payload.length) : 0);
  }

  /** 名前を切り詰めた META のフレームの大きさ（これが収まるバージョンなら、META も必ず送れる） */
  get minMetaFrameSize(): number {
    return OVERHEAD + metaPayload({ ...this.meta, name: truncateName(this.meta.name, META_NAME_MIN_BYTES) }).length;
  }

  metaFrame(maxFrameSize?: number): Uint8Array {
    const max = maxFrameSize === undefined ? undefined : maxFrameSize - OVERHEAD;
    return buildFrame(TYPE_META, this.sessionId, 0, this.total, metaPayload(this.meta, max));
  }

  dataFrame(seq: number): Uint8Array {
    return buildFrame(TYPE_DATA, this.sessionId, seq, this.total, this.chunk(seq));
  }

  repairFrame(index: number): Uint8Array {
    const payload = encodeRepair((i) => this.chunk(i), this.total, this.chunkSize, this.sessionId, index);
    return buildFrame(TYPE_REPAIR, this.sessionId, index, this.total, payload);
  }
}

export function randomSessionId(): number {
  return crypto.getRandomValues(new Uint32Array(1))[0];
}

/** payload と圧縮方式が決まった状態から計画を作る */
export async function planFromPayload(
  packed: Packed,
  compression: Compression,
  payload: Uint8Array,
  chunkSize: number,
  sessionId: number,
  appVersion: string,
): Promise<Plan> {
  const meta: Meta = {
    name: packed.name,
    mode: packed.mode,
    compression,
    raw_size: packed.raw.length,
    payload_size: payload.length,
    chunk_size: chunkSize,
    total: totalChunks(payload.length, chunkSize),
    sha256_raw: await sha256Hex(packed.raw),
    sha256_payload: compression === "none" ? "" : await sha256Hex(payload),
    mtime: packed.mode === "file" ? packed.mtime : null,
    app_version: appVersion,
  };
  if (compression === "none") meta.sha256_payload = meta.sha256_raw;
  return new Plan(sessionId, meta, payload, chunkSize);
}

/** zlib（レベル 9）で圧縮し、95% を下回れば採用する（Python 版 packer.choose_compression の zlib だけ版） */
export function chooseCompression(raw: Uint8Array): [Compression, Uint8Array] {
  if (raw.length) {
    const z = zlibSync(raw, { level: 9 });
    if (z.length < raw.length * COMPRESSION_RATIO) return ["zlib", z];
  }
  return ["none", raw];
}

export async function makePlan(packed: Packed, chunkSize: number, appVersion: string, sessionId = randomSessionId()): Promise<Plan> {
  const [compression, payload] = chooseCompression(packed.raw);
  return planFromPayload(packed, compression, payload, chunkSize, sessionId, appVersion);
}
