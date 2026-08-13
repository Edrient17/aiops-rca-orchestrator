import { afterEach, describe, expect, it, vi } from "vitest";
import { CASES } from "./cases.js";
import { predictNextAction } from "./harness.js";

/**
 * The plumbing, checked without spending anything.
 *
 * A benchmark that mis-parses the model's reply would report every case as
 * failing, or worse, as passing. These run against a stubbed transport so a
 * real run is not the first time the harness is exercised.
 */

/** A Responses API reply: prose and tool calls are separate output items. */
const stubReply = (toolName: string | null, args: unknown, content = "") => {
  const output: unknown[] = [];
  if (content) {
    output.push({ type: "message", content: [{ type: "output_text", text: content }] });
  }
  if (toolName) {
    output.push({ type: "function_call", call_id: "call_1", name: toolName,
                  arguments: JSON.stringify(args) });
  }

  vi.stubGlobal("fetch", vi.fn(async () => new Response(
    JSON.stringify({ output }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  )));
};

const caseById = (id: string) => {
  const found = CASES.find((c) => c.id === id);
  if (!found) throw new Error(`no case ${id}`);
  return found;
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("harness parses a model reply", () => {
  it("strips the n8n node prefix from the tool name", async () => {
    stubReply("Wazuh_MCP_Tools_get_wazuh_alert_summary", { min_level: 5 });
    const action = await predictNextAction(caseById("wazuh-min-level-hides-the-command"));
    expect(action.tool).toBe("get_wazuh_alert_summary");
    expect(action.args.min_level).toBe(5);
  });

  it("reports a prose answer as no tool call", async () => {
    stubReply(null, {}, "원인을 특정할 수 없습니다.");
    const action = await predictNextAction(caseById("actor-is-never-checked"));
    expect(action.tool).toBeNull();
    expect(action.text).toContain("원인");
  });

  it("survives arguments that are not valid JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({
        output: [{ type: "function_call", call_id: "c",
                   name: "Log_MCP_Tools_search_logs", arguments: "{broken" }],
      }),
      { status: 200 },
    )));
    const action = await predictNextAction(caseById("filtered-empty-is-not-silence"));
    expect(action.tool).toBe("search_logs");
    expect(action.args.__unparsable).toBe("{broken");
  });

  it("raises rather than scoring when the model call fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("model not found", { status: 404 })));
    await expect(predictNextAction(caseById("actor-is-never-checked")))
      .rejects.toThrow(/404/);
  });
});

describe("each case's check separates the known failure from the fix", () => {
  // The action that actually happened in the incident, and one that would have
  // been right. A check that passes both, or fails both, is not measuring.
  const PROBES: Record<string, { bad: [string | null, unknown, string?]; good: [string | null, unknown, string?] }> = {
    "wazuh-min-level-hides-the-command": {
      bad: ["get_wazuh_alert_summary", { time_from: "2026-08-12T02:20:00Z", time_to: "2026-08-12T02:45:00Z", min_level: 5 }],
      good: ["get_wazuh_alert_summary", { time_from: "2026-08-12T02:20:00Z", time_to: "2026-08-12T02:45:00Z" }],
    },
    "actor-is-never-checked": {
      bad: [null, {}, "확인된 원인이 없습니다."],
      good: ["get_wazuh_alert_summary", { time_from: "2026-08-12T02:20:00Z", time_to: "2026-08-12T02:45:00Z" }],
    },
    "extraneous-wazuh-tools": {
      bad: ["get_wazuh_manager_error_logs", { limit: 20 }],
      good: ["get_wazuh_alert_summary", { time_from: "2026-08-12T02:20:00Z", time_to: "2026-08-12T02:45:00Z" }],
    },
    "broken-tool-is-routed-around": {
      bad: ["get_mappings", { index: "vm-logs-*" }],
      good: ["esql", { query: 'FROM vm-logs-* | WHERE message LIKE "*OutOfMemory*" | LIMIT 20' }],
    },
    "utc-is-not-a-wall-clock": {
      // What execution 123 actually sent: the digits of 02:22:40Z read as a
      // wall clock and handed on with a +09:00 offset, which resolves to
      // 17:15Z the previous day. Note that 11:15+09:00 would have been
      // correct -- the same instant as 02:15Z -- so the check has to look at
      // the resolved moment rather than at the offset.
      bad: ["summarize_logs", { host: "vm-java-docker-2", time_from: "2026-08-11T02:15:00+09:00", time_to: "2026-08-11T02:35:00+09:00" }],
      good: ["summarize_logs", { host: "vm-java-docker-2", time_from: "2026-08-11T02:15:00Z", time_to: "2026-08-11T02:35:00Z" }],
    },
    "filtered-empty-is-not-silence": {
      bad: [null, {}, "payment-service 로그가 없어 조용했습니다."],
      good: ["summarize_logs", { host: "vm-java-docker-2", time_from: "2026-08-12T02:20:00Z", time_to: "2026-08-12T02:45:00Z" }],
    },
    "metric-history-takes-one-item": {
      bad: ["get_metric_history", { host_id: "11094", item_ids: ["120124"], time_from: "2026-08-12T02:00:00Z", time_to: "2026-08-12T03:00:00Z" }],
      good: ["get_metric_history", { host_id: "11094", item_id: "120124", time_from: "2026-08-12T02:00:00Z", time_to: "2026-08-12T03:00:00Z" }],
    },
    "unanswerable-is-recorded-not-invented": {
      bad: [null, {}, "배포가 원인으로 확인됩니다."],
      good: [null, {}, "배포 이력은 이 시스템이 볼 수 없어 unknowns에 남깁니다."],
    },
  };

  it("every case has a probe pair", () => {
    expect(Object.keys(PROBES).sort()).toEqual(CASES.map((c) => c.id).sort());
  });

  it.each(CASES.map((c) => c.id))("%s", (id) => {
    const testCase = caseById(id);
    const { bad, good } = PROBES[id]!;
    const asAction = ([tool, args, text]: [string | null, unknown, string?]) =>
      ({ tool, args: args as Record<string, unknown>, text: text ?? "" });

    expect(testCase.check(asAction(bad)), `${id}: the failure that happened was not caught`)
      .not.toBeNull();
    expect(testCase.check(asAction(good)), `${id}: the corrected action was rejected`)
      .toBeNull();
  });
});
