import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Ajv2020 } from "ajv/dist/2020.js";
import { describe, expect, it } from "vitest";

function loadSchema(name: string): object {
  return JSON.parse(
    readFileSync(resolve(process.cwd(), "..", "schemas", name), "utf8"),
  ) as object;
}

function schemaValidator(): Ajv2020 {
  const ajv = new Ajv2020({ strict: true, allErrors: true });
  ajv.addFormat("date-time", {
    type: "string",
    validate: (value: string) => !Number.isNaN(Date.parse(value)),
  });
  return ajv;
}

describe("JSON Schema drafts", () => {
  it.each([
    "parsed-request.schema.json",
    "evidence-package.schema.json",
    "rca-report.schema.json",
  ])("compiles %s as Draft 2020-12", (name) => {
    const ajv = schemaValidator();
    expect(() => ajv.compile(loadSchema(name))).not.toThrow();
  });

  function parsedRequest(hostQueries: string[]): Record<string, unknown> {
    return {
      schema_version: "0.1.0",
      request_id: "REQ-20260730-Ev123",
      parse_status: "ready",
      request_type: "incident_rca",
      host_queries: hostQueries,
      anchor_time: "2026-07-30T10:30:00+09:00",
      timezone: "Asia/Seoul",
      incident_description: "Java 프로세스 중단",
      incident_type_hint: "process_failure",
      user_intent: "RCA 보고서 작성",
      initial_window_hint: {
        before_minutes: 15,
        after_minutes: 15,
      },
      allow_dynamic_expansion: true,
      ambiguities: [],
      original_question: "10시 30분 Java-test 장애 보고서 작성해줘",
    };
  }

  it("accepts the minimum ready parsed request", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(loadSchema("parsed-request.schema.json"));

    expect(validate(parsedRequest(["Java-test"]))).toBe(true);
    expect(validate.errors).toBeNull();
  });

  // One request may name several hosts. The analyzer used to reject that as
  // unsupported, so the shape has to allow it before the prompt can.
  it("accepts a request naming several hosts, and one naming none", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(loadSchema("parsed-request.schema.json"));

    expect(validate(parsedRequest(["web-01", "web-02", "db-01"]))).toBe(true);
    expect(validate(parsedRequest([]))).toBe(true);
  });

  it("rejects the single-host shape it replaced", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(loadSchema("parsed-request.schema.json"));
    const { host_queries: _dropped, ...rest } = parsedRequest(["Java-test"]);

    expect(validate({ ...rest, host_query: "Java-test" })).toBe(false);
  });

  it("carries every resolved host through the evidence package", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(loadSchema("evidence-package.schema.json"));
    const queryContext = {
      hosts: [
        { host: "web-01", host_id: "10084" },
        { host: "web-02", host_id: "10085" },
      ],
      timezone: "Asia/Seoul",
      anchor_time: "2026-07-30T10:30:00+09:00",
    };

    expect(
      validate({
        schema_version: "0.1.0",
        request: {
          request_id: "REQ-20260730-Ev123",
          original_question: "web-01, web-02 확인해줘",
          requested_by: "U1",
        },
        query_context: queryContext,
        investigation: {
          initial_window: {
            from: "2026-07-30T10:15:00+09:00",
            to: "2026-07-30T10:45:00+09:00",
          },
          final_window: {
            from: "2026-07-30T10:15:00+09:00",
            to: "2026-07-30T10:45:00+09:00",
          },
          iterations: 2,
          tool_calls: [],
          expansion_reasons: [],
          stop_reason: "충분한 증거 확보",
          limit_reached: false,
        },
        observed_failure_mode: "web-01에서 디스크 사용률 상승",
        evidence: [],
        confirmed_facts: [],
        hypotheses: [],
        unknowns: [],
      }),
    ).toBe(true);
    expect(validate.errors).toBeNull();
  });

  // An investigation that resolved nothing has no findings to report, so the
  // package must not claim to describe hosts it never reached.
  it("requires at least one resolved host in the evidence package", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(loadSchema("evidence-package.schema.json"));

    expect(
      validate({
        schema_version: "0.1.0",
        request: {
          request_id: "R",
          original_question: "q",
          requested_by: "U1",
        },
        query_context: {
          hosts: [],
          timezone: "Asia/Seoul",
          anchor_time: "2026-07-30T10:30:00+09:00",
        },
        investigation: {
          initial_window: { from: "2026-07-30T10:15:00+09:00", to: "2026-07-30T10:45:00+09:00" },
          final_window: { from: "2026-07-30T10:15:00+09:00", to: "2026-07-30T10:45:00+09:00" },
          iterations: 1,
          tool_calls: [],
          expansion_reasons: [],
          stop_reason: "s",
          limit_reached: false,
        },
        observed_failure_mode: "m",
        evidence: [],
        confirmed_facts: [],
        hypotheses: [],
        unknowns: [],
      }),
    ).toBe(false);
  });

  it("reports on every host it covered", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(loadSchema("rca-report.schema.json"));
    const report = (hosts: string[]) => ({
      schema_version: "0.1.0",
      title: "web 계층 디스크 사용률 상승",
      executive_summary: "web-01과 web-02에서 디스크 사용률이 상승했습니다.",
      incident: {
        hosts,
        severity: null,
        started_at: null,
        recovered_at: null,
        duration_seconds: null,
        observed_failure_mode: "디스크 사용률 상승",
      },
      impact: { confirmed: [], unconfirmed: [] },
      timeline: [],
      confirmed_facts: [],
      related_signals: [],
      root_cause_candidates: [],
      recovery: [],
      immediate_actions: [],
      preventive_actions: [],
      additional_data_required: [],
      limitations: [],
    });

    expect(validate(report(["web-01", "web-02"]))).toBe(true);
    expect(validate(report(["web-01"]))).toBe(true);
    expect(validate(report([]))).toBe(false);
  });
});
