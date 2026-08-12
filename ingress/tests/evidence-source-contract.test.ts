import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Ajv2020 } from "ajv/dist/2020.js";
import { describe, expect, it } from "vitest";

/**
 * An evidence source is described in three places that have to agree: the
 * `source` enum, the `evidence_type` enum, and the `evidence_id` pattern.
 * Adding a source means touching all three, and nothing about editing one
 * suggests the others exist.
 *
 * Wazuh was added to `source` alone. Every field validated, so nothing failed
 * at review time -- but any package actually citing Wazuh would have been
 * rejected on the id pattern, discarding a finished investigation for a prefix
 * the reader had never been told about. The collector reached the tool, found
 * the operator command, and would have lost it at the door.
 *
 * Each row below is one source as it is really emitted. A source that cannot
 * produce a valid package fails here instead.
 */
const SOURCES = [
  {
    name: "zabbix event",
    evidence_id: "zbx:event:11094:down-2026-08-12T02:33:00Z",
    evidence_type: "event",
    source: "zabbix",
    search_query: null,
  },
  {
    name: "zabbix metric",
    evidence_id: "zbx:metric:119845:1786460400-1786523259-1h",
    evidence_type: "metric_summary",
    source: "zabbix",
    search_query: null,
  },
  {
    name: "log summary",
    evidence_id: "log:summary:vm-1:2026-08-12T02:00:00Z-02:45:00Z",
    evidence_type: "log_summary",
    source: "elasticsearch",
    search_query: 'host.name:"vm-1"',
  },
  {
    name: "log lines",
    evidence_id: "log:lines:vm-1:payment-service-stop",
    evidence_type: "log_lines",
    source: "elasticsearch",
    search_query: 'host.name:"vm-1"',
  },
  {
    name: "wazuh audit alerts",
    evidence_id: "wazuh:alerts:vm-java-docker-2:docker-compose-stop",
    evidence_type: "audit_alerts",
    source: "wazuh",
    search_query: null,
  },
];

function packageWith(evidence: Record<string, unknown>) {
  return {
    schema_version: "0.1.0",
    request: { request_id: "R", original_question: "q", requested_by: "U1" },
    query_context: {
      hosts: [{ host: "vm-java-docker-2", host_id: "11094" }],
      timezone: "Asia/Seoul",
      anchor_time: "2026-08-12T11:33:00+09:00",
    },
    investigation: {
      initial_window: { from: "2026-08-12T11:30:00+09:00", to: "2026-08-12T11:40:00+09:00" },
      final_window: { from: "2026-08-12T11:30:00+09:00", to: "2026-08-12T11:40:00+09:00" },
      iterations: 1,
      tool_calls: [],
      expansion_reasons: [],
      stop_reason: "s",
      limit_reached: false,
    },
    observed_failure_mode: "payment-service 중단",
    evidence: [{
      summary: "ubuntu가 docker compose stop payment-service를 실행함",
      observed_at: "2026-08-12T11:33:33+09:00",
      window: { from: "2026-08-12T11:30:00+09:00", to: "2026-08-12T11:40:00+09:00" },
      resource_ids: { host_id: "11094", event_id: null, trigger_id: null, item_id: null },
      metric: null,
      data_quality: { data_source: "logs", partial: false },
      tool_call_id: "t1",
      ...evidence,
    }],
    confirmed_facts: [],
    hypotheses: [],
    unknowns: [],
  };
}

describe("every evidence source can produce a valid package", () => {
  const ajv = new Ajv2020({ strict: true, allErrors: true });
  ajv.addFormat("date-time", {
    type: "string",
    validate: (value: string) => !Number.isNaN(Date.parse(value)),
  });
  const validate = ajv.compile(
    JSON.parse(
      readFileSync(resolve(process.cwd(), "..", "schemas", "evidence-package.schema.json"), "utf8"),
    ) as object,
  );

  it.each(SOURCES)("accepts $name", (row) => {
    const { name, ...evidence } = row;
    const ok = validate(packageWith(evidence));
    expect(
      ok,
      `${name} was rejected: ${JSON.stringify(validate.errors)}`,
    ).toBe(true);
  });

  // The id prefix is what routes a report footnote to the right system, so a
  // source whose prefix the pattern does not know would either be rejected or,
  // worse, be given another system's link.
  it.each(SOURCES)("$name uses an id prefix the pattern names", (row) => {
    const prefix = row.evidence_id.split(":")[0];
    expect(["zbx", "log", "wazuh"]).toContain(prefix);
  });

  it("still refuses a source the schema does not name", () => {
    expect(validate(packageWith({
      evidence_id: "pinpoint:trace:vm-1:slow-checkout",
      evidence_type: "audit_alerts",
      source: "pinpoint",
      search_query: null,
    }))).toBe(false);
  });
});
