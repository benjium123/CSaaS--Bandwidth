import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: {
    port: 5173,
    // Same-origin in dev, so CORS never bites.
    proxy: {
      "/api": { target: "http://127.0.0.1:8080", changeOrigin: true },
      "/healthz": { target: "http://127.0.0.1:8080", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    /**
     * Vitest's default is 5000ms, and that is too tight for this suite rather than too
     * generous. Evidence, not a guess: `AgentPage > creates, renames, sets a default and
     * deletes an assistant end to end` runs in ~850ms on an idle machine and blew past
     * 5000ms during a full 86-file parallel run. Re-running all of src/pages at a
     * deliberately tight 1200ms budget produced SIX failures, and every single one was
     * "Test timed out" - not one assertion failure, not one "unable to find". So the
     * failures under load are wall-clock budget, not races: the tests are correct and
     * simply lose the CPU to 85 sibling files.
     *
     * 20s is chosen so the genuinely long end-to-end page tests have headroom on a loaded
     * CI runner while a real hang still fails the run rather than hanging it. This does NOT
     * paper over a race - a race produces an assertion failure, which no timeout can hide.
     */
    testTimeout: 20000,
  },
});
