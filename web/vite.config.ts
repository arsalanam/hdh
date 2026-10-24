import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: proxy the API routes to the FastAPI backend (hdh serve-agent, :8100).
// Build: emit static assets into dist/, which the same FastAPI runtime serves.
// Every route the SPA calls must be here, or in dev it hits Vite and 404s:
// /ask (+ /ask/stream), /me, /threads, /conversations (+ /{id}), /notes/upload,
// /dictations (saves a recorded encounter).
const API_ROUTES = [
  "/ask",
  "/me",
  "/patients",
  "/threads",
  "/conversations",
  "/notes",
  "/dictations",
  "/health",
];

// The media service runs separately (hdh serve-media, :8200). /stt covers both
// the async upload and the /stt/stream WebSocket (ws:true), /tts voices the
// agent's spoken answer, and /capabilities lets the SPA show or hide voice
// controls. VITE_MEDIA_URL overrides in prod.
const MEDIA_ROUTES = ["/stt", "/tts", "/capabilities"];

const api = (p: string) => [p, { target: "http://127.0.0.1:8100", changeOrigin: true }] as const;
const media = (p: string) => [p, { target: "http://127.0.0.1:8200", changeOrigin: true, ws: true }] as const;

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries([...API_ROUTES.map(api), ...MEDIA_ROUTES.map(media)]),
  },
  build: { outDir: "dist", emptyOutDir: true },
});
