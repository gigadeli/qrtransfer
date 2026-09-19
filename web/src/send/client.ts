/** frameWorker への依頼を Promise で待てるようにする */
import type { WorkerRequest, WorkerResponse } from "./frameWorker";

type Pending = { resolve: (r: WorkerResponse) => void; reject: (e: Error) => void };

// 各依頼の型から id を除いたもの（id はここで振る）
type Omit2<T> = T extends unknown ? Omit<T, "id"> : never;

export class FrameClient {
  private worker: Worker | null = null;
  private nextId = 1;
  private readonly pending = new Map<number, Pending>();

  /** Worker は最初の依頼のときに作る（止めた後に依頼が来たら作り直す） */
  private ensure(): Worker {
    if (this.worker) return this.worker;
    const worker = new Worker(new URL("./frameWorker.ts", import.meta.url), { type: "module" });
    worker.onmessage = (ev: MessageEvent<WorkerResponse>) => {
      const p = this.pending.get(ev.data.id);
      if (!p) return;
      this.pending.delete(ev.data.id);
      if (ev.data.type === "error") p.reject(new Error(ev.data.message));
      else p.resolve(ev.data);
    };
    worker.onerror = (e) => {
      const err = new Error(`送信の準備でエラーが発生しました: ${e.message}`);
      for (const p of this.pending.values()) p.reject(err);
      this.pending.clear();
    };
    this.worker = worker;
    return worker;
  }

  request<T extends WorkerResponse["type"]>(req: Omit2<WorkerRequest>): Promise<Extract<WorkerResponse, { type: T }>> {
    const id = this.nextId++;
    const worker = this.ensure();
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (r: WorkerResponse) => void, reject });
      worker.postMessage({ ...req, id } as WorkerRequest);
    });
  }

  terminate(): void {
    this.worker?.terminate();
    this.worker = null;
    for (const p of this.pending.values()) p.reject(new Error("中止しました"));
    this.pending.clear();
  }
}
