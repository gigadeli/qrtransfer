import { useEffect, useRef } from "react";

const MAX_CELLS = 2000;
const MAX_CELL_PX = 16;
const TARGET_HEIGHT = 110;

/** 受信済みのチャンク（緑）と未受信（灰）のマス目。多い場合は 1 マスに複数チャンクをまとめる。 */
export function ChunkMap({ bitmap, total, version }: { bitmap: Uint8Array; total: number; version: number }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const draw = () => {
      const dpr = window.devicePixelRatio || 1;
      const width = canvas.clientWidth;
      const perCell = Math.max(1, Math.ceil(total / MAX_CELLS));
      const cells = Math.ceil(total / perCell);
      // 高さが TARGET_HEIGHT 程度に収まる大きさのマスにする
      const cell = Math.max(3, Math.min(MAX_CELL_PX, Math.floor(Math.sqrt((width * TARGET_HEIGHT) / Math.max(1, cells)))));
      const perRow = Math.max(1, Math.min(cells, Math.floor(width / cell)));
      const rows = Math.ceil(cells / perRow);
      const height = Math.max(cell, rows * cell);
      canvas.style.height = `${height}px`;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      const ctx = canvas.getContext("2d")!;
      ctx.scale(dpr, dpr);
      const style = getComputedStyle(canvas);
      const done = style.getPropertyValue("--chunk-done").trim() || "#22c55e";
      const partial = style.getPropertyValue("--chunk-partial").trim() || "#a7e3b8";
      const none = style.getPropertyValue("--chunk-none").trim() || "#e5e7eb";
      const gap = cell >= 8 ? 2 : 1;
      const ox = Math.floor((width - perRow * cell) / 2);
      for (let i = 0; i < cells; i++) {
        let got = 0;
        const end = Math.min(total, (i + 1) * perCell);
        for (let k = i * perCell; k < end; k++) got += bitmap[k];
        const ratio = got / (end - i * perCell);
        ctx.fillStyle = ratio >= 1 ? done : ratio > 0 ? partial : none;
        const x = ox + (i % perRow) * cell;
        const y = Math.floor(i / perRow) * cell;
        const s = cell - gap;
        if (s >= 5) {
          ctx.beginPath();
          ctx.roundRect(x, y, s, s, Math.min(3, s / 4));
          ctx.fill();
        } else {
          ctx.fillRect(x, y, s, s);
        }
      }
    };
    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas);
    return () => ro.disconnect();
  }, [bitmap, total, version]);

  return <canvas ref={ref} className="chunk-map" aria-label="受信状況" />;
}
