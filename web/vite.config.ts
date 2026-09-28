/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const target = process.env.QIS_URL ?? "http://127.0.0.1:8080";

// The gateway rejects non-GET admin calls whose Origin is not its own host, so the dev proxy
// rewrites Origin to the target (cookies stay first-party on the dev server origin).
const proxy = {
  target,
  changeOrigin: true,
  configure: (p: { on: (ev: string, fn: (req: { setHeader: (k: string, v: string) => void; getHeader: (k: string) => unknown }) => void) => void }) => {
    p.on("proxyReq", (req) => {
      if (req.getHeader("origin")) req.setHeader("origin", target);
    });
  },
};

export default defineConfig({
  base: "/admin/",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../qis/web/dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 700,
  },
  server: {
    port: 5173,
    proxy: {
      "/admin/api": proxy,
      "/v1": proxy,
      "/healthz": proxy,
      "/readyz": proxy,
      "/docs": proxy,
      "/openapi.json": proxy,
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
