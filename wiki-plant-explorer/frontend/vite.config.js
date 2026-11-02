import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Two pages: the app, and the animated demo of it (demo.html)
  build: { rollupOptions: { input: { main: resolve(__dirname, "index.html"), demo: resolve(__dirname, "demo.html") } } },
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.API_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
