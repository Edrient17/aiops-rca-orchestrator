import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Ajv2020 } from "ajv/dist/2020.js";
import { describe, expect, it } from "vitest";

/**
 * The evidence schema and the log MCP are one contract held in two
 * repositories, and the collector is told to copy data_quality across
 * verbatim. Because the schema forbids unknown keys, a field added on the
 * server side alone does not degrade -- it invalidates the whole package, and
 * a finished investigation is discarded for a key the reader had never heard
 * of. That has now happened twice, to empty_because_filtered and to
 * omitted_from_middle, both times after a comment in the server's source said
 * not to let it.
 *
 * A comment cannot fail a build. This can: the field list below is the shape
 * the server emits, and the schema has to keep accepting all of it.
 */
const LOG_DATA_QUALITY_FIELDS = {
  // summarize_logs
  data_source: "logs",
  formats: { spring: 1467, syslog: 78, timestamped: 3, unrecognised: 1 },
  unlevelled_lines: 78,
  sampled_fraction: 0.9515,
  // search_logs
  scanned: 85,
  matched_after_level_filter: 42,
  messages_truncated: 3,
  omitted_from_middle: 35,
  // both
  empty_because_filtered: { lines_in_window: 45744, matched_by_filters: 0 },
  partial: true,
};

function schemaValidator(): Ajv2020 {
  const ajv = new Ajv2020({ strict: true, allErrors: true });
  ajv.addFormat("date-time", {
    type: "string",
    validate: (value: string) => !Number.isNaN(Date.parse(value)),
  });
  return ajv;
}

describe("the log MCP's data_quality contract", () => {
  const validate = schemaValidator().compile(
    JSON.parse(
      readFileSync(resolve(process.cwd(), "..", "schemas", "evidence-package.schema.json"), "utf8"),
    ) as object,
  );

  const withDataQuality = (data_quality: unknown) => ({
    schema_version: "0.1.0",
    request: { request_id: "R", original_question: "q", requested_by: "U1" },
    query_context: {
      hosts: [{ host: "vm-1", host_id: "11082" }],
      timezone: "Asia/Seoul",
      anchor_time: "2026-08-11T11:22:00+09:00",
    },
    investigation: {
      initial_window: { from: "2026-08-11T11:00:00+09:00", to: "2026-08-11T12:00:00+09:00" },
      final_window: { from: "2026-08-11T11:00:00+09:00", to: "2026-08-11T12:00:00+09:00" },
      iterations: 1,
      tool_calls: [],
      expansion_reasons: [],
      stop_reason: "s",
      limit_reached: false,
    },
    observed_failure_mode: "컨테이너 중단",
    evidence: [{
      evidence_id: "log:lines:vm-1:stop",
      evidence_type: "log_lines",
      source: "elasticsearch",
      summary: "중지 명령 한 줄",
      observed_at: "2026-08-11T11:22:45+09:00",
      window: { from: "2026-08-11T11:22:00+09:00", to: "2026-08-11T11:29:00+09:00" },
      resource_ids: { host_id: "11082", event_id: null, trigger_id: null, item_id: null },
      metric: null,
      data_quality,
      search_query: 'host.name:"vm-1"',
      tool_call_id: "t1",
    }],
    confirmed_facts: [],
    hypotheses: [],
    unknowns: [],
  });

  it("accepts every field the server puts in the object, together", () => {
    expect(validate(withDataQuality(LOG_DATA_QUALITY_FIELDS))).toBe(true);
    expect(validate.errors).toBeNull();
  });

  // One field at a time, so a failure names the field that broke rather than
  // reporting that the whole object no longer fits.
  it.each(Object.keys(LOG_DATA_QUALITY_FIELDS).filter((k) => k !== "data_source" && k !== "partial"))(
    "accepts %s",
    (field) => {
      const minimal = {
        data_source: "logs",
        partial: false,
        [field]: LOG_DATA_QUALITY_FIELDS[field as keyof typeof LOG_DATA_QUALITY_FIELDS],
      };
      expect(validate(withDataQuality(minimal))).toBe(true);
    },
  );

  // The permissiveness is bounded: a key nobody has defined still fails, which
  // is what makes the check above worth running.
  it("still refuses a key the contract does not name", () => {
    expect(validate(withDataQuality({ ...LOG_DATA_QUALITY_FIELDS, invented_field: 1 }))).toBe(false);
  });
});
