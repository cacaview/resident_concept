import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies nothing; the backend (127.0.0.1:8010) enables CORS
// for http://127.0.0.1:5173, so the UI calls it directly.
export default defineConfig({
  plugins: [react()],
});
