/**
 * 受信したフレームを組み立てる（Python 版 assembler.Assembler と同じ判定。データはメモリ上に持つ）。
 *
 * - 最初に届いたフレームのセッションを受信する。別のセッションのフレームは「別の転送」として知らせ、
 *   switchToConflict() で切り替える。完了後に届いた新しいセッションはそのまま受信を始める。
 * - META が届くまでは、最終チャンク以外の長さから chunk_size を推定して長さを検証する。
 * - 修復用フレーム（REPAIR）が届いたら、欠けたチャンクを計算で求める（META が届いてから）。
 * - 全チャンクと META がそろったら、SHA-256（結合後と伸長後）を照合し、保存用のファイルを作る。
 */
import { TooLargeError, decompress, sha256Hex } from "./codec";
import { makeZip, readTar, sanitizeFilename } from "./archive";
import { type Meta, parseMeta } from "./meta";
import { type Frame, TYPE_META, TYPE_REPAIR } from "./protocol";
import { RepairDecoder, TOTAL_LIMIT as REPAIR_TOTAL_LIMIT, repairIndices } from "./repair";

export type Status = "new" | "meta" | "dup" | "conflict" | "finished" | "invalid" | "too_large";

const CHUNK_SIZE_MIN = 200;
const CHUNK_SIZE_MAX = 2000;
const RATE_WINDOW_MS = 5000;

/**
 * このページで受信できる大きさの上限。データはすべてブラウザのメモリ上に持つため、スマホで落ちない大きさに抑える。
 * フレームの「総チャンク数」や META の「サイズ」は送信側が名乗る値なので、確保する前に必ずこの上限で確かめる
 * （偽造したフレーム 1 枚で巨大な領域を確保させられないように）。
 */
export const DEFAULT_LIMITS = {
  maxRawSize: 100 * 1024 * 1024,
  maxPayloadSize: 100 * 1024 * 1024,
};

export interface Limits {
  maxRawSize: number;
  maxPayloadSize: number;
}

export interface Rejected {
  sessionId: number;
  name?: string;
  size?: number;
  limit: number;
}

export interface ReceivedFile {
  fileName: string;
  data: Uint8Array;
  mime: string;
  /** bundle のとき: ZIP にまとめたファイル数、スキップした項目、名前が重なり連番を付けた項目 */
  fileCount?: number;
  skipped?: { name: string; reason: string }[];
  renamed?: { name: string; to: string }[];
}

export interface CompletionResult {
  ok: boolean;
  sessionId: number;
  message: string;
  meta: Meta;
  elapsedMs: number;
  file?: ReceivedFile;
}

export interface Snapshot {
  sessionId: number | null;
  total: number;
  received: number;
  meta: Meta | null;
  bitmap: Uint8Array; // 各チャンクの受信済みフラグ（0/1）
  ratePerSec: number;
  bytesPerSec: number;
  etaSec: number | null;
  conflictSessionId: number | null;
  finishing: boolean;
  result: CompletionResult | null;
  /** 大きすぎて受け付けなかった転送 */
  rejected: Rejected | null;
}

class Session {
  meta: Meta | null = null;
  chunkSize: number | null = null;
  chunks: (Uint8Array | undefined)[];
  bitmap: Uint8Array;
  received = 0;
  started = performance.now();
  times: number[] = [];
  finishing = false;
  finished = false;
  decoder: RepairDecoder | null = null;

  constructor(
    readonly sessionId: number,
    readonly total: number,
  ) {
    this.chunks = new Array(total);
    this.bitmap = new Uint8Array(total);
  }
}

/** 共有メニューに渡すときの種類。ブラウザが開いて中身を実行し得る種類（HTML・SVG など）は使わない。 */
const MIME: Record<string, string> = {
  txt: "text/plain", csv: "text/csv", json: "application/json", pdf: "application/pdf",
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp",
  zip: "application/zip", mp4: "video/mp4", mov: "video/quicktime", mp3: "audio/mpeg", md: "text/markdown",
};

export function mimeOf(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  return MIME[ext] ?? "application/octet-stream";
}

export class Assembler {
  private session: Session | null = null;
  private finishedIds = new Set<number>();
  private rejectedIds = new Set<number>();
  private conflictId: number | null = null;
  private conflictTotal = 0;
  private readonly limits: Limits;
  private readonly maxTotal: number;
  result: CompletionResult | null = null;
  rejected: Rejected | null = null;
  onComplete?: (r: CompletionResult) => void;
  onChange?: () => void;

  constructor(limits: Partial<Limits> = {}) {
    this.limits = { ...DEFAULT_LIMITS, ...limits };
    this.maxTotal = Math.ceil(this.limits.maxPayloadSize / CHUNK_SIZE_MIN);
  }

  private reject(sid: number, info: Omit<Rejected, "sessionId" | "limit"> = {}): Status {
    this.rejectedIds.add(sid);
    if (this.rejected?.sessionId !== sid || info.name) {
      this.rejected = { sessionId: sid, limit: this.limits.maxRawSize, ...info };
      this.onChange?.();
    }
    return "too_large";
  }

  feed(frame: Frame): Status {
    const sid = frame.sessionId;
    if (this.finishedIds.has(sid)) return "finished";
    if (this.rejectedIds.has(sid)) return "too_large";
    let s = this.session;
    if (s === null) {
      if (frame.total > this.maxTotal) return this.reject(sid);
      s = this.session = new Session(sid, frame.total);
      this.conflictId = null;
      if (this.rejected) {
        this.rejected = null;
        this.onChange?.();
      }
    } else if (sid !== s.sessionId) {
      if (s.finished) {
        this.reset();
        return this.feed(frame); // 完了表示中に新しい転送が来たら、そのまま受信する
      }
      if (frame.total > this.maxTotal) return this.reject(sid);
      this.conflictId = sid;
      this.conflictTotal = frame.total;
      return "conflict";
    }
    if (s.finished || s.finishing) return "finished";
    if (frame.total !== s.total) return "invalid";
    const status =
      frame.type === TYPE_META ? this.handleMeta(s, frame)
        : frame.type === TYPE_REPAIR ? this.handleRepair(s, frame)
          : this.handleData(s, frame);
    if (status === "new" || status === "meta") {
      this.maybeComplete(s);
      this.onChange?.();
    }
    return status;
  }

  private lengthOk(s: Session, seq: number, n: number): boolean {
    if (s.meta) {
      const cs = s.meta.chunk_size;
      return seq < s.total - 1 ? n === cs : n === s.meta.payload_size - (s.total - 1) * cs;
    }
    if (s.chunkSize !== null) return seq < s.total - 1 ? n === s.chunkSize : n > 0 && n <= s.chunkSize;
    return n > 0;
  }

  private handleMeta(s: Session, frame: Frame): Status {
    if (s.meta) return "dup";
    const meta = parseMeta(frame.payload);
    if (!meta || meta.total !== s.total) return "invalid";
    if (meta.raw_size > this.limits.maxRawSize || meta.payload_size > this.limits.maxPayloadSize) {
      // 受信済みの分も捨てる（このセッションの以後のフレームは無視する）
      this.session = null;
      return this.reject(s.sessionId, { name: meta.name, size: meta.raw_size });
    }
    if (s.chunkSize !== null && s.chunkSize !== meta.chunk_size) return "invalid";
    s.meta = meta;
    s.chunkSize = meta.chunk_size;
    // META より先に届いていたチャンクの長さを検証し直す
    for (let i = 0; i < s.total; i++) {
      const c = s.chunks[i];
      if (c && !this.lengthOk(s, i, c.length)) {
        s.chunks[i] = undefined;
        s.bitmap[i] = 0;
        s.received--;
      }
    }
    return "meta";
  }

  private handleData(s: Session, frame: Frame): Status {
    const { seq, payload } = frame;
    if (seq >= s.total) return "invalid";
    if (s.bitmap[seq]) return "dup";
    if (!payload.length) return "invalid";
    if (s.chunkSize === null && seq < s.total - 1) {
      if (payload.length < CHUNK_SIZE_MIN || payload.length > CHUNK_SIZE_MAX * 4) return "invalid";
      for (let i = 0; i < s.total; i++) {
        const c = s.chunks[i];
        if (c && ((i < s.total - 1 && c.length !== payload.length) || c.length > payload.length)) return "invalid";
      }
      s.chunkSize = payload.length;
    }
    if (!this.lengthOk(s, seq, payload.length)) return "invalid";
    this.store(s, seq, payload);
    if (s.decoder) for (const [q, d] of s.decoder.addKnown(seq, payload)) this.store(s, q, d);
    return "new";
  }

  private store(s: Session, seq: number, data: Uint8Array): void {
    if (seq === s.total - 1 && s.meta) {
      // 修復で解けた最終チャンクは、埋め草（0）を除いた本来の長さにする
      data = data.subarray(0, s.meta.payload_size - (s.total - 1) * s.meta.chunk_size);
    }
    s.chunks[seq] = data;
    s.bitmap[seq] = 1;
    s.received++;
    const now = performance.now();
    s.times.push(now);
    while (s.times.length && now - s.times[0] > RATE_WINDOW_MS) s.times.shift();
  }

  /** 修復用フレーム: META が届いてから使う（チャンクの大きさが確定している必要がある）。 */
  private handleRepair(s: Session, frame: Frame): Status {
    if (!s.meta || s.total >= REPAIR_TOTAL_LIMIT || s.received === s.total) return "dup";
    const cs = s.meta.chunk_size;
    if (frame.payload.length !== cs) return "invalid";
    s.decoder ??= new RepairDecoder(s.total, cs);
    const indices = repairIndices(s.sessionId, frame.seq, s.total);
    const solved = s.decoder.addRepair(indices, frame.payload, (i) => s.chunks[i]);
    for (const [q, d] of solved) this.store(s, q, d);
    return solved.length ? "new" : "dup";
  }

  private maybeComplete(s: Session): void {
    if (!s.meta || s.received !== s.total || s.finishing || s.finished) return;
    s.finishing = true;
    void this.finalize(s).then((r) => {
      s.finishing = false;
      s.finished = true;
      s.chunks = []; // メモリを解放（結果のファイルは result に残る）
      s.decoder = null;
      this.finishedIds.add(s.sessionId);
      if (this.session === s) this.result = r;
      this.onComplete?.(r);
      this.onChange?.();
    });
  }

  private async finalize(s: Session): Promise<CompletionResult> {
    const meta = s.meta!;
    const base = { sessionId: s.sessionId, meta, elapsedMs: performance.now() - s.started };
    const fail = (message: string): CompletionResult => ({ ok: false, message, ...base });
    try {
      const payload = new Uint8Array(meta.payload_size);
      let off = 0;
      for (const c of s.chunks) {
        payload.set(c!, off);
        off += c!.length;
      }
      if (off !== meta.payload_size || (await sha256Hex(payload)) !== meta.sha256_payload) {
        return fail("NG: 結合後のデータの SHA-256 が一致しません");
      }
      let raw: Uint8Array;
      try {
        raw = await decompress(meta.compression, payload, Math.min(meta.raw_size, this.limits.maxRawSize));
      } catch (e) {
        if (e instanceof TooLargeError) return fail("NG: 伸長後のサイズが META の値を超えています");
        return fail(`NG: 伸長に失敗しました (${(e as Error).message})`);
      }
      if (raw.length !== meta.raw_size || (await sha256Hex(raw)) !== meta.sha256_raw) {
        return fail("NG: 元データの SHA-256 が一致しません");
      }
      const name = sanitizeFilename(meta.name) || "received";
      if (meta.mode === "file") {
        return { ok: true, message: "OK: SHA-256 一致", ...base, file: { fileName: name, data: raw, mime: mimeOf(name) } };
      }
      const tar = readTar(raw);
      const zip = makeZip(tar.entries);
      return {
        ok: true, message: "OK: SHA-256 一致", ...base,
        file: {
          fileName: `${name}.zip`, data: zip, mime: "application/zip",
          fileCount: tar.entries.filter((e) => !e.dir).length, skipped: tar.skipped, renamed: tar.renamed,
        },
      };
    } catch (e) {
      return fail(`NG: ${(e as Error).message}`);
    }
  }

  switchToConflict(): boolean {
    if (this.conflictId === null || this.conflictTotal > this.maxTotal) return false;
    this.session = new Session(this.conflictId, this.conflictTotal);
    this.conflictId = null;
    this.result = null;
    this.onChange?.();
    return true;
  }

  /** 今の受信を捨て、このセッションのフレームは以後無視する。 */
  discardCurrent(): void {
    if (this.session) this.finishedIds.add(this.session.sessionId);
    this.reset();
  }

  reset(): void {
    this.session = null;
    this.conflictId = null;
    this.result = null;
    this.onChange?.();
  }

  missing(): number[] {
    const s = this.session;
    if (!s) return [];
    const out: number[] = [];
    for (let i = 0; i < s.total; i++) if (!s.bitmap[i]) out.push(i);
    return out;
  }

  /** 欠落番号を "12,57-60,99" の形で返す（受信済みの印から直接作るので、件数が多くても軽い）。 */
  missingText(maxItems?: number): string {
    const s = this.session;
    return s ? rangesOfZeros(s.bitmap, maxItems) : "";
  }

  snapshot(): Snapshot {
    const s = this.session;
    if (!s) {
      return {
        sessionId: null, total: 0, received: 0, meta: null, bitmap: new Uint8Array(0), ratePerSec: 0,
        bytesPerSec: 0, etaSec: null, conflictSessionId: null, finishing: false, result: this.result,
        rejected: this.rejected,
      };
    }
    const now = performance.now();
    const recent = s.times.filter((t) => now - t <= RATE_WINDOW_MS);
    const span = recent.length >= 2 ? Math.max(1, now - recent[0]) / 1000 : 0;
    const rate = span ? recent.length / span : 0;
    const remaining = s.total - s.received;
    return {
      sessionId: s.sessionId, total: s.total, received: s.received, meta: s.meta, bitmap: s.bitmap,
      ratePerSec: rate, bytesPerSec: rate * (s.chunkSize ?? 0),
      etaSec: rate > 0 && remaining > 0 ? remaining / rate : null,
      conflictSessionId: s.finished ? null : this.conflictId, finishing: s.finishing, result: this.result,
      rejected: this.rejected,
    };
  }
}

/** bitmap の 0 の位置を区間表記にする。maxItems を超える分は ",…" にまとめる。 */
export function rangesOfZeros(bitmap: Uint8Array, maxItems?: number): string {
  const parts: string[] = [];
  const n = bitmap.length;
  let i = 0;
  while (i < n) {
    if (bitmap[i]) {
      i++;
      continue;
    }
    if (maxItems !== undefined && parts.length >= maxItems) {
      parts.push("…");
      break;
    }
    const start = i;
    while (i < n && !bitmap[i]) i++;
    parts.push(start === i - 1 ? String(start) : `${start}-${i - 1}`);
  }
  return parts.join(",");
}
