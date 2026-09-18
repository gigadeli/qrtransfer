import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { prepareZXingModule } from "zxing-wasm/reader";

export const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), "fixtures");

export function fixturesReady(): boolean {
  return existsSync(join(FIXTURES, "frames.json"));
}

export function readJson<T>(name: string): T {
  return JSON.parse(readFileSync(join(FIXTURES, name), "utf-8")) as T;
}

export function b64(s: string): Uint8Array {
  return new Uint8Array(Buffer.from(s, "base64"));
}

let prepared = false;
/** テストでは CDN ではなく、node_modules の WebAssembly を読み込む。 */
export function useLocalWasm(): void {
  if (prepared) return;
  const wasm = readFileSync(join(FIXTURES, "..", "..", "node_modules", "zxing-wasm", "dist", "reader", "zxing_reader.wasm"));
  prepareZXingModule({ overrides: { wasmBinary: wasm.buffer.slice(wasm.byteOffset, wasm.byteOffset + wasm.byteLength) } });
  prepared = true;
}

export interface FixtureCase {
  id: string;
  sessionId: number;
  compression: string;
  mode: "file" | "bundle";
  name: string;
  rawSha256: string;
  files: Record<string, string> | null;
  skipped: number;
  meta: string;
  data: string[];
}
