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
