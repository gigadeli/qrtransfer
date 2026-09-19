/** 送信の設定（この端末のブラウザに覚える）。既定値と上下限はパソコン版の settings.py と同じ。 */
import { CHUNK_SIZE_DEFAULT, CHUNK_SIZE_MAX, CHUNK_SIZE_MIN } from "../lib/meta";
import type { Ecc } from "../lib/qrcode";
import { RATIO_DEFAULT, RATIO_MAX, RATIO_MIN } from "../lib/repair";

export const FPS_MIN = 1;
export const FPS_MAX = 30;
export const FPS_DEFAULT = 6;
export const CODES_MAX = 4;

export type Method = "repair" | "classic";

export interface SendSettings {
  method: Method; // repair: 修復用 QR を付ける（取りこぼしても読み続ければ完了する） / classic: 従来方式（欠落番号を再送）
  fps: number;
  codes: number; // 同時に表示する QR の数
  chunkSize: number;
  ecc: Ecc;
  repairRatio: number; // 修復用 QR の枚数（全チャンク数に対する %）
}

/** 送信方式に合う誤り訂正（修復用 QR を使うなら L。読み損ねた QR は修復用 QR で補えるので、1 枚に多く入れる） */
export function recommendedEcc(method: Method): Ecc {
  return method === "repair" ? "l" : "m";
}

export const DEFAULTS: SendSettings = {
  method: "repair",
  fps: FPS_DEFAULT,
  codes: 1,
  chunkSize: CHUNK_SIZE_DEFAULT,
  ecc: recommendedEcc("repair"),
  repairRatio: RATIO_DEFAULT,
};

const KEY = "qrtransfer.send";

const clampInt = (v: unknown, min: number, max: number, def: number) =>
  typeof v === "number" && Number.isFinite(v) ? Math.min(max, Math.max(min, Math.round(v))) : def;

export function normalize(s: Partial<SendSettings>): SendSettings {
  const method: Method = s.method === "classic" ? "classic" : "repair";
  return {
    method,
    fps: clampInt(s.fps, FPS_MIN, FPS_MAX, DEFAULTS.fps),
    codes: clampInt(s.codes, 1, CODES_MAX, DEFAULTS.codes),
    chunkSize: clampInt(s.chunkSize, CHUNK_SIZE_MIN, CHUNK_SIZE_MAX, DEFAULTS.chunkSize),
    ecc: s.ecc && ["l", "m", "q", "h"].includes(s.ecc) ? s.ecc : recommendedEcc(method),
    repairRatio: clampInt(s.repairRatio, RATIO_MIN, RATIO_MAX, DEFAULTS.repairRatio),
  };
}

export function loadSettings(): SendSettings {
  try {
    return normalize(JSON.parse(localStorage.getItem(KEY) ?? "{}") as Partial<SendSettings>);
  } catch {
    return { ...DEFAULTS };
  }
}

export function saveSettings(s: SendSettings): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* 保存できなくても送信には関係ない */
  }
}
