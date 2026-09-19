/** META（転送するファイルの情報）。Python 版 packer.parse_meta と同じ検証を行う。 */

export type Mode = "file" | "bundle";
export type Compression = "none" | "zlib" | "lzma";

export interface Meta {
  name: string;
  mode: Mode;
  compression: Compression;
  raw_size: number;
  payload_size: number;
  chunk_size: number;
  total: number;
  sha256_raw: string;
  sha256_payload: string;
  mtime?: number | null;
  app_version?: string;
}

const isInt = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v);
const isSha = (v: unknown) => typeof v === "string" && v.length === 64;

export const CHUNK_SIZE_DEFAULT = 800;
export const CHUNK_SIZE_MIN = 200;
export const CHUNK_SIZE_MAX = 2000;
/** META が収まるバージョンを決めるときに確保するファイル名の長さ（Python 版 packer.META_NAME_MIN_BYTES） */
export const META_NAME_MIN_BYTES = 64;

const utf8 = new TextEncoder();
const byteLen = (s: string) => utf8.encode(s).length;

/** META の JSON（Python 版 packer._json_bytes と同じ。キーの順・区切り・非 ASCII をそのまま出す点も同じ） */
export function metaJson(meta: Meta): Uint8Array {
  return utf8.encode(JSON.stringify(meta));
}

function splitExt(name: string): [string, string] {
  // Python の os.path.splitext と同じ（先頭のドットは拡張子とみなさない）
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || name.slice(0, dot).split("").every((c) => c === ".")) return [name, ""];
  return [name.slice(0, dot), name.slice(dot)];
}

/** UTF-8 で maxBytes 以下になるように、拡張子を残して切り詰める（Python 版 packer.truncate_name） */
export function truncateName(name: string, maxBytes: number): string {
  if (byteLen(name) <= maxBytes) return name;
  let [stem, ext] = splitExt(name);
  if (byteLen(ext) > Math.floor(maxBytes / 2)) ext = "";
  const budget = maxBytes - byteLen(ext) - byteLen("~");
  if (budget <= 0) {
    let out = "";
    for (const ch of name) {
      if (byteLen(out + ch) > maxBytes) break;
      out += ch;
    }
    return out;
  }
  let out = "";
  for (const ch of stem) {
    if (byteLen(out + ch) > budget) break;
    out += ch;
  }
  return `${out}~${ext}`;
}

/** META の JSON を作る。maxBytes に収まらない場合は name を切り詰める（Python 版 packer.meta_payload） */
export function metaPayload(meta: Meta, maxBytes?: number): Uint8Array {
  let data = metaJson(meta);
  if (maxBytes === undefined || data.length <= maxBytes) return data;
  const base = metaJson({ ...meta, name: "" }).length;
  let budget = maxBytes - base;
  if (budget < 1) throw new RangeError("META does not fit even with an empty name");
  while (budget >= 1) {
    data = metaJson({ ...meta, name: truncateName(meta.name, budget) });
    if (data.length <= maxBytes) return data;
    budget -= Math.max(1, data.length - maxBytes);
  }
  throw new RangeError("META does not fit");
}

export function totalChunks(payloadSize: number, chunkSize: number): number {
  return payloadSize > 0 ? Math.ceil(payloadSize / chunkSize) : 0;
}

export function parseMeta(payload: Uint8Array): Meta | null {
  let m: Record<string, unknown>;
  try {
    m = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(payload));
  } catch {
    return null;
  }
  if (!m || typeof m !== "object" || Array.isArray(m)) return null;
  const ok =
    typeof m.name === "string" &&
    (m.mode === "file" || m.mode === "bundle") &&
    (m.compression === "none" || m.compression === "zlib" || m.compression === "lzma") &&
    isInt(m.raw_size) && m.raw_size >= 0 &&
    isInt(m.payload_size) && m.payload_size >= 0 &&
    isInt(m.chunk_size) && m.chunk_size > 0 &&
    isInt(m.total) &&
    m.total === totalChunks(m.payload_size as number, m.chunk_size as number) &&
    isSha(m.sha256_raw) &&
    isSha(m.sha256_payload) &&
    (m.mtime === undefined || m.mtime === null || typeof m.mtime === "number");
  return ok ? (m as unknown as Meta) : null;
}
