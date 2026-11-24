import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The hub serves these assets under a strict CSP (script-src 'self'; style-src 'self'),
// so the build must emit only external module scripts and stylesheets.
export default defineConfig({
  base: "/",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    modulePreload: { polyfill: false },
    assetsInlineLimit: 0,
  },
});
