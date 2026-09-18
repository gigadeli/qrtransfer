# QRTransfer 受信ページ（スマホ用）

iPhone / Android のブラウザで、パソコンの QRTransfer が表示する QR を読み取ってファイルを受信するページです。
公開先: https://gigadeli.github.io/qrtransfer/

- デスクトップ版と同じプロトコル（`src/qrtransfer/protocol.py`）・同じ照合（SHA-256）で受信します。
- 送信側が「修復用 QR を混ぜる」方式なら、取りこぼしがあっても読み続けるだけで完了します（欠落番号の入力は不要）。
- 受信したファイルが画像・動画・音声・テキストなら、受信完了の画面でそのまま確認できます（`src/lib/preview.ts`）。
  HTML・SVG など、ブラウザがページとして開いて中のスクリプトを動かし得る種類は表示しません。端末のブラウザが対応していない形式（例: 一部の動画コーデック）は表示されません。
  動画・音声は、iPhone の Safari でも再生できるように、Service Worker 経由の URL（範囲指定に対応）で渡します（`src/lib/media.ts`・`scripts/sw.js`）。
  データはブラウザのキャッシュに一時的に置き、表示を閉じたときとページを開き直したときに消します。
- iPhone / iPad ではカメラに 60fps を必須として頼みます（「希望」として頼むと 30fps にされるため）。応じられないカメラでは通常の頼み方に戻します。
  画面下部に、カメラが実際に出している解像度と fps を表示します。
- フォルダ（bundle）は ZIP にまとめ直して保存します（iPhone の「ファイル」アプリでタップすると展開できます）。
- QR の読み取りは zxing-cpp の WebAssembly 版（デスクトップ版と同じ部品）。読めないときは QR の周辺をシャープ化して読み直します。
- 受信中のデータはブラウザのメモリ上にだけ持ちます（ページを閉じると消えます）。受信できるのは 100MB まで（転送量・伸長後とも）で、超える転送は受け付けずにお知らせを出します。実用的には数 MB までが目安です。
- 悪意のある QR への対策: フレームが名乗る総チャンク数・サイズは確保前に上限で確かめ、伸長は META の値を超えた時点で打ち切ります。保存（ダウンロード）は常に `application/octet-stream` で行い、受信した HTML などがこのサイトのページとして開かれないようにしています。
- フォルダ内で名前が重なるファイル（大文字と小文字の違い、使えない文字の置き換えなど）は「名前 (2)」のように連番を付けて残します。65,536 個を超えるファイルは ZIP64 で保存します。
- カメラは HTTPS のページでしか使えません。
- 一度開くと、ページと読み取り部品を端末に保存し、以後はオフライン（機内モードなど）でも開いて受信できます。ホーム画面に追加すると、アプリのように全画面で開けます。
  - 仕組み: ビルド時に `vite.config.ts` の `offline()` が、出力したすべてのファイルを保存する Service Worker（`dist/sw.js`、元は `scripts/sw.js`）を書き出します。
  - 更新: 通信できるときはページを開くたびに最新版を読み込み、保存内容も新しい版に入れ替えます（1 つ前の版も念のため残します）。

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
| `src/lib/repair.ts` | 修復用フレームの復元（Python 版 `repair.py` と同じ決まり） |
| `src/lib/codec.ts` | 伸長（zlib / xz）と SHA-256 |
| `src/lib/archive.ts` | tar の読み取り（危険なパスは除外）と ZIP の作成 |
| `src/lib/qr.ts` | QR の読み取り（zxing-wasm）と画像補正 |
| `src/decodeWorker.ts` | 読み取りを別スレッドで行う Web Worker |
| `src/useScanner.ts` | カメラの取り込み |
| `scripts/sw.js` | オフライン用の Service Worker（ビルド時に保存するファイルの一覧を書き込む） |
| `test/interop.test.ts` | Python 版で作ったフレーム・QR 画像を受信できるかの確認 |
