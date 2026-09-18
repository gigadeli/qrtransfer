/**
 * 開発用: テスト用の QR 画像（test/fixtures/qr_XX.png）を順に表示する「偽のカメラ映像」。
 * `npm run dev` で開き、URL に ?demo を付けると、カメラの代わりにこれを使う（本番のビルドには含まれない）。
 */
export async function demoStream(fps = 6): Promise<MediaStream> {
  const info = (await (await fetch("/test/fixtures/qr.json")).json()) as { count: number };
  const images = await Promise.all(
    Array.from({ length: info.count }, (_, i) => {
      const img = new Image();
      img.src = `/test/fixtures/qr_${String(i).padStart(2, "0")}.png`;
      return img.decode().then(() => img);
    }),
  );
  const canvas = document.createElement("canvas");
  canvas.width = 1280;
  canvas.height = 720;
  const ctx = canvas.getContext("2d")!;
  let i = 0;
  const draw = () => {
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    const img = images[i++ % images.length];
    const size = canvas.height * 0.9;
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(img, (canvas.width - size) / 2, (canvas.height - size) / 2, size, size);
  };
  draw();
  setInterval(draw, 1000 / fps);
  return canvas.captureStream(30);
}
