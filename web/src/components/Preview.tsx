import { useEffect, useMemo, useState } from "react";
import type { ReceivedFile } from "../lib/assembler";
import { putMedia } from "../lib/media";
import { previewOf, previewText } from "../lib/preview";

/** 受信したファイルの内容をページ上に表示する（画像・動画・音声・テキスト）。表示できない種類では何も出さない。 */
export function Preview({ file }: { file: ReceivedFile }) {
  const info = useMemo(() => previewOf(file.fileName), [file]);
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  const [viaWorker, setViaWorker] = useState(true); // 動画・音声は Service Worker 経由の URL を先に試す

  useEffect(() => {
    setFailed(false);
    setViaWorker(true);
  }, [file]);

  useEffect(() => {
    if (!info || info.kind === "text") return;
    let alive = true;
    let release = () => {};
    const useBlob = () => {
      // blob の URL は <img> / <video> / <audio> に渡すだけで、ページとして開くことはない
      const u = URL.createObjectURL(new Blob([file.data as BlobPart], { type: info.mime }));
      release = () => URL.revokeObjectURL(u);
      setUrl(u);
    };
    if (info.kind === "image" || !viaWorker) {
      useBlob();
    } else {
      void putMedia(file.data, file.fileName, info.mime).then((m) => {
        if (!alive) {
          m?.release();
          return;
        }
        if (m) {
          release = m.release;
          setUrl(m.url);
        } else {
          useBlob();
        }
      });
    }
    return () => {
      alive = false;
      release();
      setUrl(null);
    };
  }, [file, info, viaWorker]);

  const text = useMemo(() => (info?.kind === "text" ? previewText(file.data) : null), [file, info]);

  if (!info || failed) return null;
  if (text) {
    return (
      <pre className="preview text">
        {text.text}
        {text.truncated && "\n…"}
      </pre>
    );
  }
  if (!url) return null;
  // この端末のブラウザが対応していない形式。Service Worker 経由で失敗したときは blob: の URL でもう一度試す
  const onError = () => (viaWorker && info.kind !== "image" ? setViaWorker(false) : setFailed(true));
  return (
    <div className="preview">
      {info.kind === "image" && <img src={url} alt={file.fileName} onError={onError} />}
      {info.kind === "video" && <video src={url} controls playsInline preload="metadata" onError={onError} />}
      {info.kind === "audio" && <audio src={url} controls preload="metadata" onError={onError} />}
    </div>
  );
}
