import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { syncTemplates } from "../src/template-sync.js";
import type { ReportTemplate, ReportTemplateBody } from "../src/types.js";

function template(overrides: Record<string, unknown> = {}) {
  return {
    title: "장애 RCA 보고서",
    description: "사건 하나의 원인을 조사해 달라는 요청일 때 고른다",
    collection: {
      host_selector: { mode: "from_question" },
      window: { policy: "standard" },
    },
    output: {
      sections: [{ id: "summary", heading: "요약", instruction: "3문장 이내로." }],
    },
    ...overrides,
  };
}

/** Just enough of the repository for the sync to act against. */
function fakeRepository(existing: string[] = []) {
  const rows = new Map<string, ReportTemplate>(
    existing.map((id) => [id, { template_id: id, version: 1 } as ReportTemplate]),
  );
  return {
    rows,
    listTemplates: vi.fn(async () => [...rows.values()]),
    deleteTemplate: vi.fn(async (id: string) => rows.delete(id)),
    saveTemplate: vi.fn(async (id: string, _body: ReportTemplateBody) => {
      const created = !rows.has(id);
      rows.set(id, { template_id: id, version: 1 } as ReportTemplate);
      return { version: 1, changed: created, created };
    }),
  };
}

describe("template sync", () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "aiops-templates-"));
  });
  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  const write = (name: string, body: unknown) =>
    writeFileSync(join(dir, name), JSON.stringify(body), "utf8");

  it("adds a template by adding a file", async () => {
    write("incident-rca.json", template({ template_id: "incident_rca" }));
    const repository = fakeRepository();

    const result = await syncTemplates({
      repository: repository as never,
      directory: dir,
    });

    expect(result.created).toEqual(["incident_rca"]);
    expect(repository.saveTemplate).toHaveBeenCalledOnce();
    // The id it saved under comes from the file, not the filename.
    expect(repository.saveTemplate.mock.calls[0]?.[0]).toBe("incident_rca");
  });

  it("removes a template whose file is gone, so the table matches the directory", async () => {
    write("incident-rca.json", template({ template_id: "incident_rca" }));
    const repository = fakeRepository(["incident_rca", "retired_report"]);

    const result = await syncTemplates({
      repository: repository as never,
      directory: dir,
    });

    expect(result.removed).toEqual(["retired_report"]);
    expect(repository.deleteTemplate).toHaveBeenCalledWith("retired_report");
    expect([...repository.rows.keys()]).toEqual(["incident_rca"]);
  });

  // An unmounted volume and a deliberately empty directory look identical from
  // here, and one of them would take the fallback template with it.
  it("never removes anything when it read no files at all", async () => {
    const repository = fakeRepository(["incident_rca", "monthly_capacity_report"]);
    const log = vi.fn();

    const result = await syncTemplates({
      repository: repository as never,
      directory: dir,
      log,
    });

    expect(result.skippedRemoval).toBe(true);
    expect(result.removed).toEqual([]);
    expect(repository.deleteTemplate).not.toHaveBeenCalled();
    expect(log).toHaveBeenCalledWith("template_sync_no_files", expect.anything());
  });

  // Startup fails on a bad file, which holds the deploy rather than letting a
  // half-updated registry serve the next question.
  it("writes nothing when any file is invalid", async () => {
    write("good.json", template({ template_id: "good_report" }));
    write("bad.json", template({ template_id: "bad_report", output: { sections: [] } }));
    const repository = fakeRepository();

    await expect(
      syncTemplates({ repository: repository as never, directory: dir }),
    ).rejects.toThrow(/bad\.json/);
    expect(repository.saveTemplate).not.toHaveBeenCalled();
  });

  it("refuses two files claiming the same id", async () => {
    write("a.json", template({ template_id: "same_id" }));
    write("b.json", template({ template_id: "same_id" }));
    const repository = fakeRepository();

    await expect(
      syncTemplates({ repository: repository as never, directory: dir }),
    ).rejects.toThrow(/same_id/);
    expect(repository.saveTemplate).not.toHaveBeenCalled();
  });

  it("reports an unchanged file as unchanged rather than an update", async () => {
    write("incident-rca.json", template({ template_id: "incident_rca" }));
    const repository = fakeRepository(["incident_rca"]);

    const result = await syncTemplates({
      repository: repository as never,
      directory: dir,
    });

    expect(result).toMatchObject({ created: [], updated: [], unchanged: ["incident_rca"] });
  });

  it("fails loudly when the directory is not there", async () => {
    const repository = fakeRepository();

    await expect(
      syncTemplates({ repository: repository as never, directory: join(dir, "missing") }),
    ).rejects.toThrow(/Cannot read the template directory/);
  });
});
