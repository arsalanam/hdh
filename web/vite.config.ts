import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: proxy the API routes to the FastAPI backend (hdh serve-agent, :8100).
// Build: emit static assets into dist/, which the same FastAPI runtime serves.
const API_ROUTES = ["/ask", "/health", "/conversations"];

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(
      API_ROUTES.map((p) => [p, { target: "http://127.0.0.1:8100", changeOrigin: true }]),
    ),
  },
  build: { outDir: "dist", emptyOutDir: true },
});
