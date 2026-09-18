/**
 * tar の読み取り（bundle モード）と ZIP の作成。
 *
 * スマホではフォルダを直接保存できないため、受信した tar の中身を ZIP（無圧縮）にまとめ直して保存する。
 * iPhone の「ファイル」アプリは ZIP をタップすると展開できる。
 * 危険なパス（絶対パス・".."・ドットだけの名前）は Python 版 extractor と同じ基準でスキップする。
 */
import { crc32 } from "./crc32";

export interface ArchiveEntry {
  path: string; // "/" 区切り、無害化済み
  dir: boolean;
  data: Uint8Array;
  mtime: number; // 秒
}

export interface TarResult {
  entries: ArchiveEntry[];
  skipped: { name: string; reason: string }[];
}

const INVALID_CHARS = /[<>:"|?*\x00-\x1f]/g;
const RESERVED = new Set([
  "CON", "PRN", "AUX", "NUL",
  ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`),
]);
const MAX_COMPONENT_LEN = 200;

/** ファイル名の 1 要素を、Windows / iOS で使える名前にする（Python 版 sanitize_component と同じ）。 */
export function sanitizeComponent(name: string): string {
  let n = name.replace(INVALID_CHARS, "_").replace(/[/\\]/g, "_").replace(/[ .]+$/, "");
  if (!n) n = "_";
  const stem = n.split(".", 1)[0].replace(/ +$/, "");
  if (RESERVED.has(stem.toUpperCase())) n = "_" + n;
  if (n.length > MAX_COMPONENT_LEN) {
    const dot = n.lastIndexOf(".");
    const ext = dot > 0 ? n.slice(dot, dot + 20) : "";
    const base = dot > 0 ? n.slice(0, dot) : n;
    n = base.slice(0, MAX_COMPONENT_LEN - ext.length) + ext;
  }
  return n;
}

export function sanitizeFilename(name: string): string {
  return sanitizeComponent(name.replace(/[/\\]/g, "_"));
}

/** アーカイブ内のパスを検証して無害化する。危険なら null。 */
export function splitMemberPath(name: string): string[] | null {
  if (!name || name.includes("\x00")) return null;
  const norm = name.replace(/\\/g, "/");
  if (norm.startsWith("/") || /^[A-Za-z]:/.test(norm)) return null;
  const parts = norm.split("/").filter((p) => p !== "" && p !== ".");
  if (!parts.length || parts.some((p) => p === ".." || p.replace(/[ .]+$/, "") === "")) return null;
  return parts.map(sanitizeComponent);
}

// ---------------------------------------------------------------------------- tar
const decoder = new TextDecoder("utf-8");

function cstr(buf: Uint8Array, off: number, len: number): string {
  let end = off;
  while (end < off + len && buf[end] !== 0) end++;
  return decoder.decode(buf.subarray(off, end));
}

function octal(buf: Uint8Array, off: number, len: number): number {
  if (buf[off] & 0x80) {
    // base-256（大きな値）
    let v = 0;
    for (let i = 1; i < len; i++) v = v * 256 + buf[off + i];
    return v;
  }
  const s = cstr(buf, off, len).trim();
  return s ? parseInt(s, 8) : 0;
}

function parsePax(data: Uint8Array): Record<string, string> {
  // "長さ key=value\n" の繰り返し（長さはバイト数）
  const out: Record<string, string> = {};
  let off = 0;
  while (off < data.length) {
    let sp = off;
    while (sp < data.length && data[sp] !== 0x20) sp++;
    const len = parseInt(decoder.decode(data.subarray(off, sp)), 10);
    if (!len || len <= 0) break;
    const rec = decoder.decode(data.subarray(sp + 1, off + len - 1));
    const eq = rec.indexOf("=");
    if (eq > 0) out[rec.slice(0, eq)] = rec.slice(eq + 1);
    off += len;
  }
  return out;
}

/** tar（ustar / PAX / GNU longname）を読む。通常ファイルとフォルダ以外はスキップする。 */
export function readTar(tar: Uint8Array): TarResult {
  const entries: ArchiveEntry[] = [];
  const skipped: TarResult["skipped"] = [];
  const seen = new Set<string>();
  let off = 0;
  let pax: Record<string, string> = {};
  let globalPax: Record<string, string> = {};
  let longName: string | null = null;
  while (off + 512 <= tar.length) {
    const h = tar.subarray(off, off + 512);
    if (h.every((b) => b === 0)) break;
    const size = octal(h, 124, 12);
    const type = String.fromCharCode(h[156] || 0x30);
    const dataStart = off + 512;
    const data = tar.subarray(dataStart, dataStart + size);
    off = dataStart + Math.ceil(size / 512) * 512;
    if (type === "x") {
      pax = parsePax(data);
      continue;
    }
    if (type === "g") {
      globalPax = { ...globalPax, ...parsePax(data) };
      continue;
    }
    if (type === "L") {
      longName = cstr(data, 0, data.length);
      continue;
    }
    let name = cstr(h, 0, 100);
    const magic = cstr(h, 257, 6);
    if (magic.startsWith("ustar")) {
      const prefix = cstr(h, 345, 155);
      if (prefix) name = `${prefix}/${name}`;
    }
    const attrs = { ...globalPax, ...pax };
    if (attrs.path) name = attrs.path;
    if (longName) name = longName;
    const mtime = attrs.mtime ? Math.floor(parseFloat(attrs.mtime)) : octal(h, 136, 12);
    pax = {};
    longName = null;

    const isDir = type === "5";
    const isFile = type === "0" || type === "\0" || type === "7";
    if (!isDir && !isFile) {
      skipped.push({ name, reason: "通常のファイル・フォルダではありません" });
      continue;
    }
    const parts = splitMemberPath(name);
    if (!parts) {
      skipped.push({ name, reason: "安全でないパス" });
      continue;
    }
    const path = parts.join("/");
    if (seen.has(path + (isDir ? "/" : ""))) continue;
    seen.add(path + (isDir ? "/" : ""));
    entries.push({ path, dir: isDir, data: isDir ? new Uint8Array(0) : data.slice(), mtime });
  }
  return { entries, skipped };
}

// ---------------------------------------------------------------------------- zip
function dosDateTime(sec: number): [number, number] {
  const d = new Date((sec || Date.now() / 1000) * 1000);
  const year = Math.max(1980, d.getFullYear());
  const time = (d.getHours() << 11) | (d.getMinutes() << 5) | Math.floor(d.getSeconds() / 2);
  const date = ((year - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
  return [time, date];
}

/** 無圧縮（stored）の ZIP を作る。ファイル名は UTF-8（フラグ 0x0800）。 */
export function makeZip(entries: ArchiveEntry[]): Uint8Array {
  const enc = new TextEncoder();
  const locals: Uint8Array[] = [];
  const centrals: Uint8Array[] = [];
  let offset = 0;
  for (const e of entries) {
    const name = enc.encode(e.dir ? e.path + "/" : e.path);
    const data = e.dir ? new Uint8Array(0) : e.data;
    const crc = crc32(data);
    const [time, date] = dosDateTime(e.mtime);
    const local = new Uint8Array(30 + name.length);
    const lv = new DataView(local.buffer);
    lv.setUint32(0, 0x04034b50, true);
    lv.setUint16(4, 20, true);
    lv.setUint16(6, 0x0800, true);
    lv.setUint16(8, 0, true);
    lv.setUint16(10, time, true);
    lv.setUint16(12, date, true);
    lv.setUint32(14, crc, true);
    lv.setUint32(18, data.length, true);
    lv.setUint32(22, data.length, true);
    lv.setUint16(26, name.length, true);
    local.set(name, 30);
    const central = new Uint8Array(46 + name.length);
    const cv = new DataView(central.buffer);
    cv.setUint32(0, 0x02014b50, true);
    cv.setUint16(4, 20, true);
    cv.setUint16(6, 20, true);
    cv.setUint16(8, 0x0800, true);
    cv.setUint16(12, time, true);
    cv.setUint16(14, date, true);
    cv.setUint32(16, crc, true);
    cv.setUint32(20, data.length, true);
    cv.setUint32(24, data.length, true);
    cv.setUint16(28, name.length, true);
    cv.setUint32(38, e.dir ? 0x10 : 0, true);
    cv.setUint32(42, offset, true);
    central.set(name, 46);
    locals.push(local, data);
    centrals.push(central);
    offset += local.length + data.length;
  }
  const centralSize = centrals.reduce((n, c) => n + c.length, 0);
  const end = new Uint8Array(22);
  const ev = new DataView(end.buffer);
  ev.setUint32(0, 0x06054b50, true);
  ev.setUint16(8, entries.length, true);
  ev.setUint16(10, entries.length, true);
  ev.setUint32(12, centralSize, true);
  ev.setUint32(16, offset, true);
  const out = new Uint8Array(offset + centralSize + end.length);
  let p = 0;
  for (const part of [...locals, ...centrals, end]) {
    out.set(part, p);
    p += part.length;
  }
  return out;
}
