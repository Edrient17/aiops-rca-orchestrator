import { readFileSync, readdirSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
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
      "host_state_check",
      "incident_rca",
      "log_review",
      "monthly_capacity_report",
    ]);
    // Substitution happened: what reaches the database is a real group id.
    const monthly = files.find((f) => f.template_id === "monthly_capacity_report")!;
    const selector = monthly.collection.host_selector;
    expect(selector.mode === "host_group" && selector.group_ids).toEqual(["10"]);
  });

  it("carries every field a template file declares through to the database", async () => {
    // zod strips what it does not declare. `requires_effects` was missing from
    // the section schema once: the template files kept their declarations and
    // the sync quietly wrote sections without them, turning a guarantee off on
    // the next ingress restart with nothing to see. That field is gone, the
    // hazard is not -- so this compares the parsed sections against the files
    // rather than naming any field.
    process.env.AIOPS_MONTHLY_HOST_GROUP_ID = "10";
    const files = await readTemplateFiles(dir);

    for (const file of files) {
      const raw = JSON.parse(
        await readFile(join(dir, `${file.template_id.replace(/_/g, "-")}.json`), "utf8"),
      );
      const rawSections: Record<string, unknown>[] = raw.output.sections;
      for (const [index, section] of rawSections.entries()) {
        const parsed = file.output.sections[index] as Record<string, unknown>;
        expect(parsed.id).toBe(section.id);
        for (const key of Object.keys(section)) {
          expect(Object.keys(parsed)).toContain(key);
        }
      }
    }
  });

  it("give the analyzer descriptions it can choose between", async () => {
    // The question analyzer picks one of these by reading the descriptions and
    // nothing else. Two that read alike is a coin toss on every request, and
    // the losing template is one nobody can reach.
    process.env.AIOPS_MONTHLY_HOST_GROUP_ID = "10";
    const files = await readTemplateFiles(dir);

    const descriptions = files.map((file) => file.description);
    expect(new Set(descriptions).size).toBe(files.length);
    for (const description of descriptions) {
      expect(description.length).toBeGreaterThan(40);
    }
  });

  it("names each section once within a template", async () => {
    // A repeated id means one section silently overwrites the other, and the
    // report loses a heading the template promised.
    process.env.AIOPS_MONTHLY_HOST_GROUP_ID = "10";
    const files = await readTemplateFiles(dir);

    for (const file of files) {
      const ids = file.output.sections.map((section) => section.id);
      expect(new Set(ids).size, `${file.template_id} repeats a section id`).toBe(
        ids.length,
      );
    }
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

  // A monthly report computes its own window, so the hint has no job. Forcing a
  // value inside 5..1440 for a question spanning a month is how the analyzer
  // rejected its own output.
  it("lets the window hint be null, but still bounds it when present", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(loadSchema("parsed-request.schema.json"));
    const withHint = (initial_window_hint: unknown) => ({
      ...parsedRequest([]),
      initial_window_hint,
    });

    expect(validate(withHint(null))).toBe(true);
    expect(validate(withHint({ before_minutes: 720, after_minutes: 720 }))).toBe(true);
    // A month in minutes is what a month-scale question invites.
    expect(validate(withHint({ before_minutes: 43200, after_minutes: 43200 }))).toBe(false);
    expect(validate(withHint({ before_minutes: 720 }))).toBe(false);
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

  // An investigation that checks a cause and rules it out has found something,
  // and a hypothesis with no support and two contradictions is how that reads.
  // Requiring support here threw away a complete package -- seven pieces of
  // evidence and a correct conclusion -- over the one entry that recorded a
  // ruled-out cause.
  it("keeps a hypothesis the investigation ruled out", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(loadSchema("evidence-package.schema.json"));

    const withHypotheses = (hypotheses: unknown[]) => ({
      schema_version: "0.1.0",
      request: { request_id: "R", original_question: "q", requested_by: "U1" },
      query_context: {
        hosts: [{ host: "web-01", host_id: "10084" }],
        timezone: "Asia/Seoul",
        anchor_time: "2026-08-10T16:25:00+09:00",
      },
      investigation: {
        initial_window: { from: "2026-08-10T15:25:00+09:00", to: "2026-08-10T17:25:00+09:00" },
        final_window: { from: "2026-08-10T15:25:00+09:00", to: "2026-08-10T17:25:00+09:00" },
        iterations: 1,
        tool_calls: [],
        expansion_reasons: [],
        stop_reason: "s",
        limit_reached: false,
      },
      observed_failure_mode: "컨테이너가 종료됨",
      evidence: [],
      confirmed_facts: [],
      hypotheses,
      unknowns: [],
    });

    expect(
      validate(
        withHypotheses([
          {
            description: "CPU 또는 메모리 고갈이 원인이다",
            supporting_evidence_refs: [],
            contradicting_evidence_refs: ["zbx:metric:118233:a", "zbx:metric:118229:a"],
            confidence: "low",
          },
        ]),
      ),
    ).toBe(true);
    expect(validate.errors).toBeNull();

    // A confirmed fact is a different claim: it says something is so, and one
    // with nothing behind it is not confirmed.
    expect(
      validate({
        ...withHypotheses([]),
        confirmed_facts: [{ fact: "디스크가 찼다", evidence_refs: [] }],
      }),
    ).toBe(false);
  });

  // Not every tool reports on its own answer. get_incident_events returns no
  // data_quality at all, so evidence drawn from it has none to copy -- and an
  // investigation that invented a partial one, {partial: false}, had its whole
  // package rejected. Both ends of that have to be pinned: null is right, and
  // a half-filled object is not.
  it("takes null data_quality, and refuses a partial object", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(loadSchema("evidence-package.schema.json"));

    const withQuality = (data_quality: unknown) => ({
      schema_version: "0.1.0",
      request: { request_id: "R", original_question: "q", requested_by: "U1" },
      query_context: {
        hosts: [{ host: "web-01", host_id: "10084" }],
        timezone: "Asia/Seoul",
        anchor_time: "2026-08-11T02:30:00+09:00",
      },
      investigation: {
        initial_window: { from: "2026-08-11T02:00:00+09:00", to: "2026-08-11T03:00:00+09:00" },
        final_window: { from: "2026-08-11T02:00:00+09:00", to: "2026-08-11T03:00:00+09:00" },
        iterations: 1,
        tool_calls: [],
        expansion_reasons: [],
        stop_reason: "s",
        limit_reached: false,
      },
      observed_failure_mode: "컨테이너 중단",
      evidence: [{
        evidence_id: "zbx:event:24526244",
        evidence_type: "event",
        source: "zabbix",
        summary: "컨테이너 중단 이벤트",
        observed_at: "2026-08-11T02:22:40+09:00",
        window: { from: "2026-08-11T02:00:00+09:00", to: "2026-08-11T03:00:00+09:00" },
        resource_ids: { host_id: "10084", event_id: "24526244", trigger_id: "74899", item_id: null },
        metric: null,
        data_quality,
        tool_call_id: "e1",
      }],
      confirmed_facts: [],
      hypotheses: [],
      unknowns: [],
    });

    expect(validate(withQuality(null))).toBe(true);
    expect(validate(withQuality({ partial: false }))).toBe(false);
    expect(validate(withQuality({ data_source: "logs" }))).toBe(false);
  });

  // Logs became a second evidence source, so the package has to hold a finding
  // that has no Zabbix object behind it -- no item, no trigger, no metric --
  // while still tying it to the host whose metrics sit beside it.
  it("accepts log evidence and keeps it tied to a resolved host", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(loadSchema("evidence-package.schema.json"));
    const logEvidence = {
      evidence_id: "log:summary:test-java-docker-vm:2026-08-10T07:00:00Z-2026-08-10T07:45:00Z",
      evidence_type: "log_summary",
      source: "elasticsearch",
      summary: "07:25에 ERROR 38건이 몰렸고 전부 payment-service 호출 실패였다.",
      observed_at: "2026-08-10T07:25:18+09:00",
      window: { from: "2026-08-10T07:00:00+09:00", to: "2026-08-10T07:45:00+09:00" },
      resource_ids: { host_id: "10084", event_id: null, trigger_id: null, item_id: null },
      metric: null,
      // Every field the log MCP puts in data_quality, together. The collector is
      // told to copy that object verbatim, so any field the server adds and the
      // schema does not know rejects the whole package -- which is how
      // empty_because_filtered, added on the MCP side alone, threw away a
      // complete investigation. Keeping the full shape here means the schema
      // has to keep accepting it.
      data_quality: {
        data_source: "logs",
        partial: true,
        sampled_fraction: 0.2715,
        unlevelled_lines: 100,
        formats: { spring: 9898, syslog: 102 },
        scanned: 38,
        matched_after_level_filter: 38,
        messages_truncated: 3,
        empty_because_filtered: null,
      },
      tool_call_id: "e0a1",
    };

    const withEvidence = (evidence: unknown) => ({
      schema_version: "0.1.0",
      request: { request_id: "R", original_question: "q", requested_by: "U1" },
      query_context: {
        hosts: [{ host: "test-java-docker-vm", host_id: "10084" }],
        timezone: "Asia/Seoul",
        anchor_time: "2026-08-10T07:30:00+09:00",
      },
      investigation: {
        initial_window: { from: "2026-08-10T07:00:00+09:00", to: "2026-08-10T07:45:00+09:00" },
        final_window: { from: "2026-08-10T07:00:00+09:00", to: "2026-08-10T07:45:00+09:00" },
        iterations: 1,
        tool_calls: [],
        expansion_reasons: [],
        stop_reason: "s",
        limit_reached: false,
      },
      observed_failure_mode: "결제 호출 실패",
      evidence: [evidence],
      confirmed_facts: [],
      hypotheses: [],
      unknowns: [],
    });

    expect(validate(withEvidence(logEvidence))).toBe(true);
    expect(validate.errors).toBeNull();

    // The narrowed-miss report has to survive with a value in it, not only as
    // null: that is the case it exists for.
    expect(
      validate(
        withEvidence({
          ...logEvidence,
          data_quality: {
            ...logEvidence.data_quality,
            empty_because_filtered: { lines_in_window: 45744, matched_by_filters: 0 },
          },
        }),
      ),
    ).toBe(true);

    // The two data_quality shapes must stay distinguishable, or `oneOf` matches
    // both and the schema silently stops checking either.
    expect(
      validate(
        withEvidence({
          ...logEvidence,
          data_quality: { ...logEvidence.data_quality, data_source: "history" },
        }),
      ),
    ).toBe(false);

    // A prefix nothing produces would let a citation point at nothing.
    expect(
      validate(withEvidence({ ...logEvidence, evidence_id: "kibana:summary:x" })),
    ).toBe(false);

    // Zabbix evidence keeps its own shape: the widened source enum must not let
    // a metric finding claim it came from the log cluster.
    expect(
      validate(
        withEvidence({
          ...logEvidence,
          evidence_id: "zbx:metric:55036:a",
          evidence_type: "metric_summary",
          data_quality: {
            data_source: "logs",
            partial: false,
            sample_count: 60,
            coverage_ratio: 1,
          },
        }),
      ),
    ).toBe(false);
  });

  // A prose section has no items and a list section has no body, and there is
  // no third reading of an absent one. Demanding both discarded a finished
  // report -- twelve sections, every one of them filled -- because the writer
  // left out the half that did not apply.
  it("takes a section that omits the half it does not use", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(loadSchema("report.schema.json"));
    const report = (sections: unknown[]) => ({
      schema_version: "0.1.0",
      title: "제목",
      sections,
    });

    expect(validate(report([{ id: "summary", body: "요약입니다." }]))).toBe(true);
    expect(validate(report([{ id: "facts", items: [
      { text: "디스크가 참", label: null, evidence_refs: ["zbx:metric:1:a"], counter_evidence_refs: [] },
    ] }]))).toBe(true);
    expect(validate(report([{ id: "scope" }]))).toBe(true);

    // The id is still the one thing a section cannot do without: it is what
    // matches the section to its template heading.
    expect(validate(report([{ body: "제목 없는 칸" }]))).toBe(false);
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
