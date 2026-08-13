import { resolve } from "node:path";
import { defineConfig } from "vitest/config";

/**
 * The benchmark cases live in ../bench because they are about the agent, not
 * about this service. The config lives here because this is where the vitest
 * install is.
 *
 * It is deliberately not part of `npm test`: it calls a real model, costs real
 * tokens, and takes minutes -- none of which belongs in the loop a developer
 * runs on every save, or in a CI job that has no API key.
 */
export default defineConfig({
  // Rooted at the case directory rather than reaching into it with a glob:
  // an absolute Windows path in `include` is read as a pattern, and the
  // backslashes silently match nothing.
  root: resolve(import.meta.dirname, "..", "bench"),
  test: {
    include: ["**/*.bench.ts"],
    // One case at a time: these failures are about how the model reads a
    // trajectory, and parallel calls make a rate limit look like a regression.
    fileParallelism: false,
    maxConcurrency: 1,
    testTimeout: 180_000,
    reporters: ["verbose"],
  },
});
