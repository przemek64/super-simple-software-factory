import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const API_PORT = process.env.PORT ?? "4600";
// One UI port per project, so two factories can be watched at once. The
// launcher assigns a pair and passes them in; 4601 stays the bare default.
const UI_PORT = Number(process.env.VITE_PORT ?? 4601);

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
      "@shared": fileURLToPath(new URL("./shared", import.meta.url)),
    },
  },
  server: {
    port: UI_PORT,
    strictPort: true,   // fail loudly rather than drifting onto a neighbour's port
    proxy: {
      "/api": {
        target: `http://localhost:${API_PORT}`,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
