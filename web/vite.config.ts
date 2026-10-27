import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies the API to a running `kestrel web` on :8765.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/kestrel/web/static", emptyOutDir: true, chunkSizeWarningLimit: 900 },
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8765", ws: true } },
  },
});
