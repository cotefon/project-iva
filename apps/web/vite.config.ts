import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The app calls the FastAPI service directly at VITE_API_URL rather than through
// a dev proxy, so the browser exercises the same CORS path in development as in
// production — a misconfigured ALLOWED_ORIGINS fails here instead of only after
// deploying.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
});
