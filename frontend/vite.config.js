import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

const DEFAULT_PROXY_TARGET = "http://127.0.0.1:8000";
const tauriHost = process.env.TAURI_DEV_HOST;

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const proxyTarget = (env.VITE_PROXY_TARGET || env.VITE_API_BASE_URL || DEFAULT_PROXY_TARGET).trim();

  return {
    clearScreen: false,
    plugins: [react()],
    envPrefix: ["VITE_", "TAURI_"],
    server: {
      host: tauriHost || "0.0.0.0",
      port: 5173,
      strictPort: true,
      hmr: tauriHost
        ? {
            protocol: "ws",
            host: tauriHost,
            port: 1421,
          }
        : undefined,
      watch: {
        ignored: ["**/src-tauri/**"],
      },
      proxy: {
        "/api": {
          target: proxyTarget,
          changeOrigin: true,
        },
        "/health": {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: "dist",
      emptyOutDir: true,
      target: process.env.TAURI_ENV_PLATFORM === "windows" ? "chrome105" : "safari13",
      minify: process.env.TAURI_ENV_DEBUG ? false : "esbuild",
      sourcemap: Boolean(process.env.TAURI_ENV_DEBUG),
    },
  };
});
