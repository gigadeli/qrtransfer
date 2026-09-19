import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../styles.css";
import SendApp from "./SendApp";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <SendApp />
  </StrictMode>,
);

// 一度開けば、以後はオフラインでも開けるようにする（sw.js はビルド時に作る。受信ページと共通）
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register(`${import.meta.env.BASE_URL}sw.js`).catch((e) => console.warn(e));
  });
}
