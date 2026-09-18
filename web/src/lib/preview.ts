/**
 * 受信したファイルをページ上で確認（プレビュー）できる種類。
 *
 * 画像・動画・音声は <img> / <video> / <audio> で、テキストは文字として表示する。
 * HTML・SVG・XML などブラウザが「ページとして」開いて中のスクリプトを動かし得る種類は、
 * 画像や文書として表示しない（受信したファイルがこのサイトのページとして動くことがないように）。
 */

export type PreviewKind = "image" | "video" | "audio" | "text";

const TYPES: Record<string, [PreviewKind, string]> = {
  png: ["image", "image/png"],
  jpg: ["image", "image/jpeg"],
  jpeg: ["image", "image/jpeg"],
  gif: ["image", "image/gif"],
  webp: ["image", "image/webp"],
  avif: ["image", "image/avif"],
  bmp: ["image", "image/bmp"],
  heic: ["image", "image/heic"], // iPhone の写真（Safari で表示できる）
  heif: ["image", "image/heif"],
  mp4: ["video", "video/mp4"],
  m4v: ["video", "video/mp4"],
  mov: ["video", "video/quicktime"],
  webm: ["video", "video/webm"],
  mp3: ["audio", "audio/mpeg"],
  m4a: ["audio", "audio/mp4"],
  aac: ["audio", "audio/aac"],
  wav: ["audio", "audio/wav"],
  ogg: ["audio", "audio/ogg"],
  opus: ["audio", "audio/ogg"],
  flac: ["audio", "audio/flac"],
  txt: ["text", "text/plain"],
  md: ["text", "text/plain"],
  csv: ["text", "text/plain"],
  tsv: ["text", "text/plain"],
  json: ["text", "text/plain"],
  log: ["text", "text/plain"],
  ini: ["text", "text/plain"],
  yaml: ["text", "text/plain"],
  yml: ["text", "text/plain"],
};

/** テキストとして表示する最大バイト数（大きなファイルで画面が固まらないように） */
export const TEXT_PREVIEW_MAX = 256 * 1024;

export function previewOf(name: string): { kind: PreviewKind; mime: string } | null {
  const dot = name.lastIndexOf(".");
  if (dot < 0) return null;
  const t = TYPES[name.slice(dot + 1).toLowerCase()];
  return t ? { kind: t[0], mime: t[1] } : null;
}

/** テキストの先頭 TEXT_PREVIEW_MAX バイトを文字列にする（UTF-8。先頭の BOM は除く）。 */
export function previewText(data: Uint8Array): { text: string; truncated: boolean } {
  const truncated = data.length > TEXT_PREVIEW_MAX;
  const text = new TextDecoder("utf-8", { ignoreBOM: false }).decode(truncated ? data.subarray(0, TEXT_PREVIEW_MAX) : data);
  return { text, truncated };
}
