import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/query": {
        target: "http://localhost:8000",
        changeOrigin: true,
        selfHandleResponse: false,
        configure: (proxy) => {
          // Forward SSE chunks immediately — disable http-proxy's internal buffering.
          proxy.on("proxyRes", (proxyRes) => {
            proxyRes.socket?.setNoDelay(true);
          });
        },
      },
      "/health": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
