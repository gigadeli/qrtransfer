/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// GitHub Pages ではリポジトリ名のパス（/qrtransfer/）で公開される。ローカルでは / のまま
export default defineConfig({
  base: process.env.QRT_BASE ?? "/",
  plugins: [react()],
  worker: { format: "es" },
  build: { target: "es2022" },
  test: {
    environment: "node",
    setupFiles: ["test/setup.ts"],
    testTimeout: 30000,
  },
});
