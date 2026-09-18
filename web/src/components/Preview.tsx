import { useEffect, useMemo, useState } from "react";
import type { ReceivedFile } from "../lib/assembler";
import { previewOf, previewText } from "../lib/preview";

/** 受信したファイルの内容をページ上に表示する（画像・動画・音声・テキスト）。表示できない種類では何も出さない。 */
export function Preview({ file }: { file: ReceivedFile }) {
  const info = useMemo(() => previewOf(file.fileName), [file]);
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setFailed(false);
    if (!info || info.kind === "text") return;
    // blob の URL は <img> / <video> / <audio> に渡すだけで、ページとして開くことはない
    const u = URL.createObjectURL(new Blob([file.data as BlobPart], { type: info.mime }));
    setUrl(u);
    return () => {
      URL.revokeObjectURL(u);
      setUrl(null);
    };
  }, [file, info]);

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
  const onError = () => setFailed(true); // この端末のブラウザが対応していない形式
  return (
    <div className="preview">
      {info.kind === "image" && <img src={url} alt={file.fileName} onError={onError} />}
      {info.kind === "video" && <video src={url} controls playsInline preload="metadata" onError={onError} />}
      {info.kind === "audio" && <audio src={url} controls preload="metadata" onError={onError} />}
    </div>
  );
}
