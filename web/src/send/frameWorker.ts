/// <reference lib="webworker" />
/**
 * 送信データの準備と QR の生成を別スレッドで行う（画面の表示を止めないため）。
 * - plan: 送るものを梱包・圧縮し、転送計画を作る（大きいファイルでは数秒かかる）
 * - chunk: チャンクサイズだけを変えて計画を作り直す（梱包・圧縮はやり直さない）
 * - render: 表示順の値（META / DATA / 修復用）から、フレームを作って QR にする
 */
import { CAROUSEL_META, repairIndex } from "../lib/carousel";
import { type Meta, totalChunks } from "../lib/meta";
import { type Packed, type SendItem, Plan, makePlan, randomSessionId, packItems } from "../lib/pack";
import { type Ecc, encodeQr, packModules } from "../lib/qrcode";

/**
 * DATA・修復用フレームのマスク（固定）。8 種類を評価する手間を省くと、生成が約 6 倍速くなる。
 * 中身は圧縮済み・XOR 済みでほぼ乱雑なので、どのマスクでも読みやすさはほとんど変わらない。
 * 文字（JSON）が並ぶ META だけは、規格どおり評価して選ぶ。
 */
const DATA_MASK = 2;

export interface PlanInfo {
  meta: Meta;
  sessionId: number;
  total: number;
  fileCount: number;
  maxDataFrameSize: number;
  minMetaFrameSize: number;
}

export type WorkerRequest =
  | { type: "plan"; id: number; items: SendItem[]; chunkSize: number }
  | { type: "chunk"; id: number; chunkSize: number }
  | { type: "render"; id: number; version: number; ecc: Ecc; metaMax: number; entries: number[] };

export type WorkerResponse =
  | { type: "planned"; id: number; info: PlanInfo }
  | { type: "rendered"; id: number; entries: number[]; size: number; matrices: Uint8Array[] } // matrices は 1 ビットずつに詰めたもの
  | { type: "error"; id: number; message: string };

let packed: Packed | null = null;
let plan: Plan | null = null;
let metaFrame: { key: string; frame: Uint8Array } | null = null;

function info(p: Plan, fileCount: number): PlanInfo {
  return {
    meta: p.meta,
    sessionId: p.sessionId,
    total: p.total,
    fileCount,
    maxDataFrameSize: p.maxDataFrameSize,
    minMetaFrameSize: p.minMetaFrameSize,
  };
}

function post(msg: WorkerResponse, transfer: Transferable[] = []): void {
  (self as unknown as DedicatedWorkerGlobalScope).postMessage(msg, transfer);
}

// 依頼は届いた順に 1 つずつ処理する（準備の途中で次の準備が始まり、古い結果で上書きされないように）
let queue = Promise.resolve();
self.onmessage = (ev: MessageEvent<WorkerRequest>) => {
  queue = queue.then(() => handle(ev.data));
};

async function handle(req: WorkerRequest): Promise<void> {
  try {
    if (req.type === "plan") {
      packed = null;
      plan = null;
      packed = await packItems(req.items);
      plan = await makePlan(packed, req.chunkSize, __APP_VERSION__);
      metaFrame = null;
      post({ type: "planned", id: req.id, info: info(plan, packed.fileCount) });
    } else if (req.type === "chunk") {
      if (!plan || !packed) throw new Error("送るものが選ばれていません");
      // 圧縮済みのデータはそのままで、分け方だけを変える（別の転送として扱うので session も新しくする）
      const meta = { ...plan.meta, chunk_size: req.chunkSize, total: totalChunks(plan.payload.length, req.chunkSize) };
      plan = new Plan(randomSessionId(), meta, plan.payload, req.chunkSize);
      metaFrame = null;
      post({ type: "planned", id: req.id, info: info(plan, packed.fileCount) });
    } else {
      if (!plan) throw new Error("送るものが選ばれていません");
      const matrices: Uint8Array[] = [];
      let size = 0;
      for (const e of req.entries) {
        let frame: Uint8Array;
        const isMeta = e === CAROUSEL_META;
        if (isMeta) {
          // META は毎回同じなので作り置く（名前が長いときは QR に収まるよう切り詰める）
          const key = `${plan.sessionId}:${req.metaMax}`;
          if (metaFrame?.key !== key) metaFrame = { key, frame: plan.metaFrame(req.metaMax) };
          frame = metaFrame.frame;
        } else {
          const r = repairIndex(e);
          frame = r !== null ? plan.repairFrame(r) : plan.dataFrame(e);
        }
        const qr = encodeQr(frame, req.version, req.ecc, isMeta ? undefined : DATA_MASK);
        size = qr.size;
        matrices.push(packModules(qr.modules));
      }
      post({ type: "rendered", id: req.id, entries: req.entries, size, matrices }, matrices.map((m) => m.buffer));
    }
  } catch (e) {
    post({ type: "error", id: req.id, message: e instanceof Error ? e.message : String(e) });
  }
}
