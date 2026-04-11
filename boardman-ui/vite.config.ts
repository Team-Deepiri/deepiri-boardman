import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5176,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8090",
        changeOrigin: true,
        // Match long agent/Ollama turns (nginx uses 600s in Docker).
        timeout: 600_000,
        proxyTimeout: 600_000,
      },
    },
  },
});
