import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The prompts name tools, parameters and evidence id prefixes. None of those
 * live here, so nothing fails when one is renamed on the other side -- the
 * prompt simply goes on describing a system that no longer exists, and the
 * agent follows it into a wasted call or a rejected package.
 *
 * That has now happened twice in one day. Trimming the Wazuh router left the
 * prompt pointing at get_wazuh_rules_summary, and moving guidance out of the
 * tool descriptions left it saying "the call order is in each tool's
 * description" after the call order had been taken out of them.
 *
 * These are cheap string checks, not a schema. They catch the class of drift
 * that is invisible in review because the two sides are edited months apart.
 */
const PROMPTS = resolve(process.cwd(), "..", "prompts");
const SCHEMAS = resolve(process.cwd(), "..", "schemas");

const promptText = Object.fromEntries(
  readdirSync(PROMPTS)
    .filter((f) => f.endsWith(".md"))
    .map((f) => [f, readFileSync(resolve(PROMPTS, f), "utf8")]),
);

const allPrompts = Object.values(promptText).join("\n");

// The tools each MCP server actually routes, as deployed.
const ROUTED_TOOLS = new Set([
  // zabbix-investigation-mcp
  "find_hosts",
  "get_incident_events",
  "get_trigger_details",
  "list_relevant_metrics",
  "get_metric_summary",
  "get_metric_history",
  "get_related_events",
  "query_zabbix",
  // elasticsearch-investigation-mcp
  "summarize_logs",
  "search_logs",
  // official elasticsearch mcp
  "search",
  "esql",
  "list_indices",
  "get_mappings",
  "get_shards",
  // mcp-server-wazuh, after the router was trimmed to four
  "get_wazuh_alert_summary",
  "get_wazuh_agents",
  "get_wazuh_agent_processes",
  "get_wazuh_agent_ports",
]);

// Parameters and response fields share the verb-prefixed shape of a tool name,
// so they are named here rather than guessed at. A new one has to be added
// before this test passes, which is the point: it forces a look at whether the
// identifier is a field or a tool that no longer exists.
const NOT_TOOLS = new Set([
  "search_term",
  "search_query",
  "query_kql",
  "get_wazuh_alert_summary_result",
  "list_relevant_metrics_result",
]);

describe("prompts only name tools that exist", () => {
  const TOOLISH = /`([a-z][a-z0-9_]{3,})`/g;
  const VERBS = ["get_", "list_", "search_", "find_", "query_", "summarize_"];

  it.each(Object.keys(promptText))("%s", (file) => {
    const named = new Set<string>();
    for (const [, name] of promptText[file]!.matchAll(TOOLISH)) {
      if (VERBS.some((v) => name.startsWith(v)) && !NOT_TOOLS.has(name)) named.add(name);
    }
    const unknown = [...named].filter((n) => !ROUTED_TOOLS.has(n));
    expect(unknown, `${file} names tools that are not routed: ${unknown.join(", ")}`)
      .toEqual([]);
  });
});

describe("prompts stay consistent with the evidence schema", () => {
  const schema = readFileSync(resolve(SCHEMAS, "evidence-package.schema.json"), "utf8");

  // Every evidence_type the collector is told to emit has to be in the enum,
  // and every id prefix has to satisfy the pattern.
  it.each(["log_summary", "log_lines", "audit_alerts"])("%s is an evidence_type", (type) => {
    expect(schema).toContain(`"${type}"`);
  });

  it.each(["elasticsearch", "wazuh"])("%s is a source", (source) => {
    expect(schema).toContain(`"${source}"`);
  });

  // Found by walking rather than by path: the definition has already moved
  // once, and a test that breaks on a schema reshuffle stops being run.
  const findPattern = (node: unknown): string | null => {
    if (Array.isArray(node)) {
      for (const child of node) {
        const hit = findPattern(child);
        if (hit) return hit;
      }
      return null;
    }
    if (node && typeof node === "object") {
      const record = node as Record<string, unknown>;
      const props = record.properties as Record<string, { pattern?: string }> | undefined;
      if (props?.evidence_id?.pattern) return props.evidence_id.pattern;
      for (const child of Object.values(record)) {
        const hit = findPattern(child);
        if (hit) return hit;
      }
    }
    return null;
  };

  it.each(["log:summary", "log:lines", "wazuh:alerts", "zbx:event", "zbx:metric"])(
    "%s is accepted by the evidence_id pattern",
    (prefix) => {
      const pattern = findPattern(JSON.parse(schema));
      expect(pattern, "no evidence_id pattern found in the schema").not.toBeNull();
      expect(new RegExp(pattern!).test(`${prefix}:vm-1:x`)).toBe(true);
    },
  );
});

describe("prompts do not defer to guidance that was removed", () => {
  // Tool descriptions were reduced to describing their own tool. A prompt that
  // still sends the reader there for call order is pointing at nothing.
  it("does not claim the call order lives in the tool descriptions", () => {
    expect(allPrompts).not.toMatch(/호출 순서[^.]*도구 설명에 있다/);
  });
});
