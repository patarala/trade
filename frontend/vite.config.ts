import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backendOrigin = process.env.TRADE_API_ORIGIN ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/health": backendOrigin,
      "/api": backendOrigin,
    },
  },
});
