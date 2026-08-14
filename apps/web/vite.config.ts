import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Keeping the target configurable makes browser acceptance testing and
    // Docker/local development independent from a hard-coded API port.
    proxy: { "/api": process.env.API_PROXY_TARGET || "http://localhost:8000" }
  }
});
