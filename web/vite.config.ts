/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { type Plugin, defineConfig } from "vite";

function listFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? listFiles(join(dir, e.name)) : [join(dir, e.name)],
  );
}

/** ビルド結果のすべてのファイルを保存する Service Worker（dist/sw.js）を書き出す。オフラインで開けるように。 */
function offline(): Plugin {
  let outDir = "";
  return {
    name: "qrtransfer-offline",
    apply: "build",
    configResolved(config) {
      outDir = resolve(config.root, config.build.outDir);
    },
    closeBundle() {
      const hash = createHash("sha256");
      const files = listFiles(outDir)
        .map((f) => relative(outDir, f).split("\\").join("/"))
        .filter((f) => f !== "sw.js")
        .sort();
      for (const f of files) hash.update(f).update(readFileSync(join(outDir, f)));
      const template = readFileSync(resolve(import.meta.dirname, "scripts/sw.js"), "utf-8");
      const sw = template
        .replace("__VERSION__", JSON.stringify(hash.digest("hex").slice(0, 16)))
        .replace("__FILES__", JSON.stringify(files));
      writeFileSync(join(outDir, "sw.js"), sw);
    },
  };
}

// GitHub Pages ではリポジトリ名のパス（/qrtransfer/）で公開される。ローカルでは / のまま
export default defineConfig({
  base: process.env.QRT_BASE ?? "/",
  plugins: [react(), offline()],
  worker: { format: "es" },
  build: { target: "es2022" },
  test: {
    environment: "node",
    setupFiles: ["test/setup.ts"],
    testTimeout: 30000,
  },
});
