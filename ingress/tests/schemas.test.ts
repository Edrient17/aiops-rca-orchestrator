import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { Ajv2020 } from "ajv/dist/2020.js";
import { describe, expect, it } from "vitest";
import { readTemplateFiles } from "../src/template-sync.js";

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

// The incident RCA template is seeded as SQL, so nothing validates it on the
// way in the way the API validates an operator's. If its section ids drifted
// from the rules the API enforces, the mismatch would only surface when someone
// tried to save that same template back through it.
describe("seeded incident_rca template", () => {
  const seed = readFileSync(
    resolve(process.cwd(), "..", "database", "migrations", "007_seed_incident_rca.sql"),
    "utf8",
  );
  const ids = [...seed.matchAll(/'id',\s*'([^']*)'/g)].map((match) => match[1]!);

  it("declares sections whose ids the template API would accept", () => {
    expect(ids.length).toBeGreaterThan(0);
    for (const id of ids) {
      expect(id).toMatch(/^[a-z][a-z0-9_]{2,63}$/);
    }
    expect(new Set(ids).size).toBe(ids.length);
  });

  // The renderer withholds these unless a real problem event was found, and the
  // writer is never asked to produce them either. Getting the set wrong is how
  // an outage that never happened gets reported.
  it("gates exactly the sections that depend on an incident having occurred", () => {
    const gated = [...seed.matchAll(/'id',\s*'([^']*)'[\s\S]*?'requires_problem_event',\s*(true|false)/g)]
      .filter((match) => match[2] === "true")
      .map((match) => match[1]!);

    expect(gated.sort()).toEqual(["incident_timing", "recovery", "timeline"]);
  });
});

// The shipped templates are what a deploy installs, so they go through the
// exact path the deploy uses -- environment substitution included. Checking the
// raw JSON instead would pass on a file that startup then rejects.
describe("shipped templates", () => {
  const dir = resolve(process.cwd(), "..", "templates");

  it("all load through the same reader the sync uses", async () => {
    process.env.AIOPS_MONTHLY_HOST_GROUP_ID = "10";
    const files = await readTemplateFiles(dir);

    expect(files.length).toBeGreaterThan(0);
    expect(files.map((file) => file.template_id).sort()).toEqual([
      "incident_rca",
      "monthly_capacity_report",
    ]);
    // Substitution happened: what reaches the database is a real group id.
    const monthly = files.find((f) => f.template_id === "monthly_capacity_report")!;
    const selector = monthly.collection.host_selector;
    expect(selector.mode === "host_group" && selector.group_ids).toEqual(["10"]);
  });

  it("refuses to load when a referenced variable is unset", async () => {
    delete process.env.AIOPS_MONTHLY_HOST_GROUP_ID;

    await expect(readTemplateFiles(dir)).rejects.toThrow(
      /AIOPS_MONTHLY_HOST_GROUP_ID/,
    );
  });
});

describe("JSON Schema drafts", () => {
  it.each([
    "parsed-request.schema.json",
    "evidence-package.schema.json",
    "report.schema.json",
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

  // request_type used to be an enum. It cannot stay one: the kinds live in a
  // table an operator adds rows to, while this schema is compiled into the
  // workflow, so a fixed list could never grow. It is still shaped like a
  // template id rather than free text.
  it("accepts any template id as the request type, but not free text", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(loadSchema("parsed-request.schema.json"));
    const withType = (request_type: string) => ({
      ...parsedRequest(["Java-test"]),
      request_type,
    });

    for (const ok of ["incident_rca", "monthly_capacity_report", "x9_report"]) {
      expect(validate(withType(ok))).toBe(true);
    }
    for (const bad of ["Monthly Report", "월말보고서", "ab", "9_report", ""]) {
      expect(validate(withType(bad))).toBe(false);
    }
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

  // The report shape stopped being incident-specific: headings come from the
  // template, so the writer only fills declared sections by id.
  it("accepts a filled-in section set and rejects an undeclared shape", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(loadSchema("report.schema.json"));
    const section = (id: string, extra: Record<string, unknown> = {}) => ({
      id,
      body: null,
      items: [],
      ...extra,
    });

    expect(
      validate({
        schema_version: "0.1.0",
        title: "web 계층 디스크 사용률 상승",
        sections: [
          section("summary", { body: "디스크가 상승했습니다." }),
          section("candidates", {
            items: [
              {
                text: "로그 적재 증가",
                label: "high",
                evidence_refs: ["zbx:metric:42269:a"],
                counter_evidence_refs: [],
              },
            ],
          }),
        ],
      }),
    ).toBe(true);
    expect(validate.errors).toBeNull();

    // A heading in the output would mean the writer chose the layout.
    expect(
      validate({
        schema_version: "0.1.0",
        title: "t",
        sections: [{ ...section("summary"), heading: "요약" }],
      }),
    ).toBe(false);

    // Section ids follow the template id shape, so a free-text one is refused.
    expect(
      validate({
        schema_version: "0.1.0",
        title: "t",
        sections: [section("Summary Section")],
      }),
    ).toBe(false);

    // A report with no sections is not a report.
    expect(
      validate({ schema_version: "0.1.0", title: "t", sections: [] }),
    ).toBe(false);
  });
});
