import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// The browser stays on one origin; Vite forwards /api to residentd.
// Set RESIDENT_API_TARGET in frontend/.env.local for a local SSH tunnel.
export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), "RESIDENT_");
  const target = env.RESIDENT_API_TARGET;
  if (command === "serve" && !target) {
    throw new Error("Set RESIDENT_API_TARGET in frontend/.env.local before starting the WebUI");
  }
  return {
    plugins: [react()],
    server: target ? {
      proxy: {
        "/api": {
          target,
          changeOrigin: true,
        },
      },
    } : undefined,
  };
});
