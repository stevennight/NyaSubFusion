import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 开发时把接口代理到本地服务：uv run nyasub serve（默认 8765 端口）
const core = process.env.NYASUB_CORE ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": core,
      "/ws": { target: core.replace(/^http/, "ws"), ws: true },
    },
  },
});
