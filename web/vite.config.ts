/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { execSync } from "node:child_process";
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

/** ページに表示する版: アプリのバージョン（Python 版と共通）・コミット・ビルド日時。更新されたかを見分けられるように。 */
function appVersion(): string {
  const init = readFileSync(resolve(import.meta.dirname, "../src/qrtransfer/__init__.py"), "utf-8");
  return /__version__\s*=\s*"([^"]+)"/.exec(init)?.[1] ?? "?";
}

function buildInfo(): string {
  const version = appVersion();
  let commit = process.env.GITHUB_SHA ?? "";
  if (!commit) {
    try {
      commit = execSync("git rev-parse HEAD", { encoding: "utf-8" }).trim();
    } catch {
      commit = "";
    }
  }
  const date = new Date().toLocaleString("sv-SE", { timeZone: "Asia/Tokyo" }).slice(0, 16); // YYYY-MM-DD HH:mm（日本時間）
  return [`v${version}`, commit.slice(0, 7), date].filter(Boolean).join(" · ");
}

// GitHub Pages ではリポジトリ名のパス（/qrtransfer/）で公開される。ローカルでは / のまま
export default defineConfig({
  base: process.env.QRT_BASE ?? "/",
  plugins: [react(), offline()],
  // __APP_VERSION__ は送信する META の app_version に入れる（Python 版と同じ番号）
  define: { __BUILD_INFO__: JSON.stringify(buildInfo()), __APP_VERSION__: JSON.stringify(appVersion()) },
  worker: { format: "es" },
  // 受信（index.html）と送信（send.html）の 2 ページ
  build: {
    target: "es2022",
    rollupOptions: { input: { main: resolve(import.meta.dirname, "index.html"), send: resolve(import.meta.dirname, "send.html") } },
  },
  test: {
    environment: "node",
    setupFiles: ["test/setup.ts"],
    testTimeout: 30000,
  },
});
