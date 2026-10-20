import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Relative asset URLs: in AWS the app is served under the API Gateway stage
  // path (https://<id>.execute-api.<region>.amazonaws.com/<stage>/).
  base: "./",
  server: {
    port: 5173,
    proxy: {
      "/chat": {
        target: "http://localhost:8000",
        changeOrigin: true,
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
