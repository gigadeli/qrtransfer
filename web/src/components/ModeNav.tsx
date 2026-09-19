/** ページの見出し: アプリ名と、受信・送信のページの切り替え */
export function ModeNav({ current }: { current: "receive" | "send" }) {
  const index = current === "send" ? 1 : 0;
  return (
    <>
      <span className="logo" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M5 14v5h14v-5M12 4v11M8 11l4 4 4-4" />
        </svg>
      </span>
      <h1>QRTransfer</h1>
      <nav className="segmented modes" aria-label="ページ" style={{ "--count": 2, "--index": index } as React.CSSProperties}>
        <span className="thumb" aria-hidden="true" />
        <a href="./" aria-current={current === "receive" ? "page" : undefined}>
          受信
        </a>
        <a href="./send.html" aria-current={current === "send" ? "page" : undefined}>
          送信
        </a>
      </nav>
    </>
  );
}
