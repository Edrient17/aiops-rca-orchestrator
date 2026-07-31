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

  it("accepts the minimum ready parsed request", () => {
    const ajv = schemaValidator();
    const validate = ajv.compile(
      loadSchema("parsed-request.schema.json"),
    );
    const valid = validate({
      schema_version: "0.1.0",
      request_id: "REQ-20260730-Ev123",
      parse_status: "ready",
      request_type: "incident_rca",
      host_query: "Java-test",
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
    });

    expect(validate.errors).toBeNull();
    expect(valid).toBe(true);
  });
});
