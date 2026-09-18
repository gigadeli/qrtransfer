# QRTransfer 受信ページ（スマホ用）

iPhone / Android のブラウザで、パソコンの QRTransfer が表示する QR を読み取ってファイルを受信するページです。
公開先: https://gigadeli.github.io/qrtransfer/

- デスクトップ版と同じプロトコル（`src/qrtransfer/protocol.py`）・同じ照合（SHA-256）で受信します。
- フォルダ（bundle）は ZIP にまとめ直して保存します（iPhone の「ファイル」アプリでタップすると展開できます）。
- QR の読み取りは zxing-cpp の WebAssembly 版（デスクトップ版と同じ部品）。読めないときは QR の周辺をシャープ化して読み直します。
- 受信中のデータはブラウザのメモリ上にだけ持ちます（ページを閉じると消えます。数 MB までが目安）。
- カメラは HTTPS のページでしか使えません。

## 開発

```bash
npm install
npm run fixtures   # テスト用データを Python 版の送信処理で作る（リポジトリ直下の .venv か、segno・numpy・pillow・opencv が入った Python）
npm test
npm run dev        # http://localhost:5173/ 。?demo を付けると、カメラの代わりにテスト用の QR を流す（?demo=2 で 2 枚/秒）
npm run build
```

スマホの実機で開発中のページを試すには、カメラのために HTTPS が必要です（`npm run dev` は HTTP）。
GitHub Pages に公開されたページで確かめるのが簡単です。

## 構成

| ファイル | 内容 |
|---|---|
| `src/lib/protocol.ts` | フレームの解析、欠落番号の表記 |
| `src/lib/meta.ts` | META（ファイル名・サイズ・ハッシュなど）の検証 |
| `src/lib/assembler.ts` | フレームの組み立て、別の転送の検出、完了時の照合と保存用ファイルの作成 |
| `src/lib/codec.ts` | 伸長（zlib / xz）と SHA-256 |
| `src/lib/archive.ts` | tar の読み取り（危険なパスは除外）と ZIP の作成 |
| `src/lib/qr.ts` | QR の読み取り（zxing-wasm）と画像補正 |
| `src/decodeWorker.ts` | 読み取りを別スレッドで行う Web Worker |
| `src/useScanner.ts` | カメラの取り込み |
| `test/interop.test.ts` | Python 版で作ったフレーム・QR 画像を受信できるかの確認 |
