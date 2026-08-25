import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { envSchema } from "../src/config.js";

/**
 * Keeps docker-compose.yml honest about what ingress cannot start without.
 *
 * The two drifted apart once already. `SLACK_BOT_TOKEN` was optional in compose
 * because it was, back when n8n did the posting and the token was only used to
 * ask for a written correction; ingress took the posting over and the variable
 * became load-bearing for the acknowledgement, the clarification and the report,
 * while the compose entry and the note above it stayed as they were.
 * `SLACK_ANSWER_CHANNEL_ID` was the same shape of mistake.
 *
 * Nothing caught it, because both are set in every real .env -- the file lied
 * and the deployment worked anyway. What it cost was the failure mode: a fresh
 * install following the compose file's own `:-` would bring ingress up to a zod
 * stack trace at boot instead of being told, by name, which variable is missing.
 *
 * So this reads the requirement off the schema rather than off a list here. A
 * variable added as required is covered the day it is added.
 */

function readCompose(): string {
  return readFileSync(resolve(process.cwd(), "..", "docker-compose.yml"), "utf8");
}

/**
 * The ingress service's `environment:` entries, as written.
 *
 * Throws rather than returning nothing when the shape it expects is not there.
 * An empty map would make every assertion below pass while checking nothing,
 * which is the failure this whole file exists to prevent.
 */
function ingressEnvironment(compose: string): Map<string, string> {
  const lines = compose.split("\n");
  const service = lines.indexOf("  ingress:");
  if (service === -1) {
    throw new Error("docker-compose.yml declares no ingress service");
  }
  const block = lines.findIndex((line, index) => index > service && line === "    environment:");
  if (block === -1) {
    throw new Error("the ingress service declares no environment block");
  }

  const entries = new Map<string, string>();
  for (let index = block + 1; index < lines.length; index += 1) {
    const line = lines[index] ?? "";
    if (line.trim() === "" || line.trimStart().startsWith("#")) {
      continue;
    }
    // The block ends at the first line indented shallower than its entries.
    if (!line.startsWith("      ")) {
      break;
    }
    const entry = /^ {6}([A-Z0-9_]+):\s*(.*)$/.exec(line);
    if (entry) {
      entries.set(entry[1]!, entry[2]!.trim());
    }
  }

  if (entries.size === 0) {
    throw new Error("parsed no entries out of the ingress environment block");
  }
  return entries;
}

/**
 * Whether compose can be relied on to hand this value over non-empty.
 *
 * `${VAR:?message}` refuses to start without one. `${VAR:-fallback}` supplies
 * one. Literal text in the value carries it whatever the interpolations do --
 * DATABASE_URL is assembled from a scheme, a host and a path, so it is never
 * the empty string. `${VAR:-}` and a bare `${VAR}` are the two that can arrive
 * empty, and a required variable must not be written either way.
 */
function guaranteedNonEmpty(value: string): boolean {
  if (!value.includes("${")) {
    return value.length > 0;
  }
  if (value.replace(/\$\{[^}]*\}/g, "").trim().length > 0) {
    return true;
  }
  return [...value.matchAll(/\$\{([^}]*)\}/g)].every(
    ([, inner]) => (inner ?? "").includes(":?") || /:-\s*\S/.test(inner ?? ""),
  );
}

describe("the compose file and what ingress requires", () => {
  it("guarantees a value for every variable ingress refuses to start without", () => {
    const environment = ingressEnvironment(readCompose());

    const offenders: string[] = [];
    for (const [key, schema] of Object.entries(envSchema.shape)) {
      // A key that parses `undefined` is optional or defaulted; compose owes it
      // nothing. Everything else is a variable ingress will not boot without.
      if (schema.safeParse(undefined).success) {
        continue;
      }
      const declared = environment.get(key);
      if (declared === undefined) {
        offenders.push(`${key}: required by ingress, never passed by compose`);
      } else if (!guaranteedNonEmpty(declared)) {
        offenders.push(`${key}: required by ingress, but compose may pass it empty -- ${declared}`);
      }
    }

    expect(offenders).toEqual([]);
  });

  it("is reading a real environment block, not an empty one", () => {
    const environment = ingressEnvironment(readCompose());

    expect(environment.size).toBeGreaterThan(10);
    expect(environment.get("PORT")).toBe("8080");
  });

  it("finds at least one required variable to check", () => {
    // Guards the other direction: if schema introspection ever stopped
    // reporting required keys, the first test would pass by checking nothing.
    const required = Object.entries(envSchema.shape).filter(
      ([, schema]) => !schema.safeParse(undefined).success,
    );

    expect(required.length).toBeGreaterThan(3);
  });
});

describe("guaranteedNonEmpty", () => {
  it("accepts the two forms compose cannot hand over empty", () => {
    expect(guaranteedNonEmpty("${A:?A is required}")).toBe(true);
    expect(guaranteedNonEmpty("${A:-fallback}")).toBe(true);
  });

  it("rejects an empty default and a bare interpolation", () => {
    expect(guaranteedNonEmpty("${A:-}")).toBe(false);
    expect(guaranteedNonEmpty("${A}")).toBe(false);
  });

  it("accepts a value whose literal text survives every interpolation", () => {
    expect(guaranteedNonEmpty("postgresql://${USER:-aiops}:${PASSWORD}@postgres:5432/${DB:-aiops}")).toBe(true);
  });
});
