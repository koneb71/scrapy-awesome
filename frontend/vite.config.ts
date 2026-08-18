import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The UI is served by the Python server in production (same origin). In dev, proxy API/WS calls
// to a running `scrapy-awesome serve` (SCRAPY_AWESOME_API, default http://127.0.0.1:7788).
const api = process.env.SCRAPY_AWESOME_API ?? "http://127.0.0.1:7788";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: api, changeOrigin: true },
      "/auth": { target: api, changeOrigin: true },
      "/health": { target: api, changeOrigin: true },
      "/ws": { target: api.replace("http", "ws"), ws: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
