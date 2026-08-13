import { describe, expect, it } from "vitest";
import { CASES } from "./cases.js";
import { benchEnabled, predictNextAction } from "./harness.js";

/**
 * Planning is scored on its own, without running the investigation, because an
 * end-to-end report entangles the plan with whatever the logs happened to hold
 * that day. Each case freezes a trajectory at the point a real run went wrong
 * and asks only for the next action.
 *
 * Grouped by the APB setting so a run reads as a diagnosis rather than a list:
 * failures clustered in one setting say something different from failures
 * spread across all of them.
 */

const SETTINGS = ["step-wise", "tool-broken", "tool-extraneous", "unsolvable"] as const;

// Model answers vary between calls, so a case that passes once has not been
// shown to pass. Raised for a release check; kept low for an everyday run.
const REPEATS = Number(process.env.BENCH_REPEATS ?? 1);

describe.skipIf(!benchEnabled)("Evidence Collector planning", () => {
  for (const setting of SETTINGS) {
    const cases = CASES.filter((c) => c.kind === setting);
    if (cases.length === 0) continue;

    describe(setting, () => {
      for (const testCase of cases) {
        it(`[${testCase.guards}] ${testCase.id}`, { timeout: 180_000 }, async () => {
          const failures: string[] = [];
          for (let attempt = 0; attempt < REPEATS; attempt += 1) {
            const action = await predictNextAction(testCase);
            const reason = testCase.check(action);
            if (reason) {
              failures.push(
                `attempt ${attempt + 1}: ${reason}\n` +
                `  chose: ${action.tool ?? "(no tool call)"} ${JSON.stringify(action.args)}`,
              );
            }
          }
          expect(
            failures,
            `${testCase.id} guards ${testCase.guards}.\n` +
            `Origin: ${testCase.origin}\n\n${failures.join("\n")}`,
          ).toEqual([]);
        });
      }
    });
  }
});

describe("benchmark wiring", () => {
  it("every case is a real regression with an origin", () => {
    for (const testCase of CASES) {
      expect(testCase.origin.length, `${testCase.id} has no origin`).toBeGreaterThan(40);
    }
  });

  // A benchmark that only covers one setting stops being diagnostic: it can
  // say the plan is wrong but not what kind of wrong.
  it("covers every setting the suite claims to test", () => {
    const covered = new Set(CASES.map((c) => c.kind));
    expect([...covered].sort()).toEqual([...SETTINGS].sort());
  });

  it("reports which error categories are guarded", () => {
    const byGuard = new Map<string, string[]>();
    for (const testCase of CASES) {
      byGuard.set(testCase.guards, [...(byGuard.get(testCase.guards) ?? []), testCase.id]);
    }
    // Not an assertion so much as a printed inventory, so a reader of the run
    // sees the coverage without opening the cases file.
    for (const [guard, ids] of [...byGuard].sort()) {
      console.log(`  ${guard}: ${ids.length} case(s) — ${ids.join(", ")}`);
    }
    expect(byGuard.size).toBeGreaterThan(1);
  });
});
