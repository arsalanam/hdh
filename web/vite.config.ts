import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: proxy the API routes to the FastAPI backend (hdh serve-agent, :8100).
// Build: emit static assets into dist/, which the same FastAPI runtime serves.
// Every route the SPA calls must be here, or in dev it hits Vite and 404s:
// /ask (+ /ask/stream), /me, /threads, /conversations (+ /{id}), /notes/upload.
const API_ROUTES = ["/ask", "/me", "/threads", "/conversations", "/notes", "/health"];

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(
      API_ROUTES.map((p) => [p, { target: "http://127.0.0.1:8100", changeOrigin: true }]),
    ),
  },
  build: { outDir: "dist", emptyOutDir: true },
});
