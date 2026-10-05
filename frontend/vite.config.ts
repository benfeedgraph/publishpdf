import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// PUBLISHPDF_API lets a second dev stack (e.g. one on the local database) run beside the usual one.
const apiTarget = process.env.PUBLISHPDF_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": apiTarget, "/healthz": apiTarget },
  },
});
