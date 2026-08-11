import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dashboard talks to the CSMS on :9000. Proxying in dev means the browser
// sees one origin, so no CORS dance and the WebSocket upgrade works unchanged.
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // Recharts and its d3 dependencies are most of the bundle and change
        // far less often than our code, so they get their own cacheable chunk.
        manualChunks: {
          charts: ["recharts"],
          vendor: ["react", "react-dom", "react-router-dom", "@tanstack/react-query"],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:9000", changeOrigin: true },
      "/ws": { target: "ws://localhost:9000", ws: true },
      "/sim": {
        target: "http://localhost:9100",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/sim/, ""),
      },
    },
  },
});
