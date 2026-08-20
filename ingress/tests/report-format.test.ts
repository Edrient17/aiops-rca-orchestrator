/**
 * The ported formatter against the messages n8n actually posted.
 *
 * `Format LangGraph RCA` was the one Code node in the workflow doing work rather
 * than relaying HTTP, and porting 242 lines of it by hand is exactly the kind of
 * change that looks finished and is subtly wrong. It does not have to be taken
 * on trust: `aiops_reports` holds `rca_report` and `slack_markdown` in the same
 * row, so the port can be held against real output byte for byte.
 *
 * Run over the whole table, 49 of 52 comparable reports matched exactly. The
 * other three name a report kind since retired, so no template survives to
 * render them -- nothing differed. Reports written before 2026-08-13 are not
 * comparable at all: the node did not exist yet and the legacy pipeline had its
 * own formatter.
 *
 * The fixture keeps five of those reports, chosen to cover every branch that
 * changes the output -- all four link kinds, an empty required section, counter
 * evidence, labelled items, unlinked footnotes, and each surviving report kind.
 * Only the two branches of the package the formatter reads are stored, which is
 * itself a claim the tests below check.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { formatReport, type FormatConfig } from "../src/report-format.js";

/**
 * The addresses these messages were rendered against, replaced throughout the
 * fixture with documentation ranges and placeholder ids. The formatter copies
 * them through, so substituting input and expected output together leaves the
 * comparison exactly as strict while keeping the monitoring stack's addresses,
 * and a Slack member id, out of a public repository.
 */
const CONFIG: FormatConfig = {
  zabbixFrontendUrl: "http://192.0.2.241/zabbix",
  kibanaUrl: "http://192.0.2.105:5601",
  kibanaDataViewId: "00000007-0000-4000-8000-000000000000",
};

interface Golden {
  request_id: string;
  user_id: string;
  traits: string[];
  template_output: { sections?: unknown[] };
  package: { evidence?: unknown[]; query_context?: unknown };
  report: { title?: string; sections?: unknown[] };
  expected_markdown: string;
}

const GOLDEN: Golden[] = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("./fixtures/report-format-golden.json", import.meta.url)),
    "utf8",
  ),
);

const render = (row: Golden, config: FormatConfig = CONFIG) =>
  formatReport(
    {
      request: { request_id: row.request_id, user_id: row.user_id },
      template: { output: row.template_output as never },
      evidencePackage: row.package as never,
      report: row.report as never,
    },
    config,
  );

describe("the port against what n8n posted", () => {
  it.each(GOLDEN.map((row) => [row.request_id, row] as const))(
    "%s renders byte for byte",
    (_id, row) => {
      expect(render(row).slackMarkdown).toBe(row.expected_markdown);
    },
  );

  it("covers every branch that changes the output", () => {
    // A fixture that drifts into covering only the easy cases stops being
    // evidence of anything. These are the traits the selection was made for.
    const covered = new Set(GOLDEN.flatMap((row) => row.traits));
    for (const trait of [
      "link:graph",
      "link:event",
      "link:latest",
      "link:kibana",
      "empty-required-section",
      "counter-evidence",
      "unlinked-footnote",
      "labelled-item",
    ]) {
      expect(covered).toContain(trait);
    }
  });
});

describe("what the formatter reads", () => {
  it("needs nothing from the package but evidence and query_context", () => {
    // The fixture stores only those two branches. If rendering ever starts
    // reading hypotheses or the investigation record, every case above would
    // pass against a fixture that no longer holds the input.
    const row = GOLDEN[0]!;
    expect(Object.keys(row.package).sort()).toEqual(["evidence", "query_context"]);
    expect(render(row).slackMarkdown).toBe(row.expected_markdown);
  });

  it("counts the footnotes it numbered", () => {
    const row = GOLDEN.find((item) => item.traits.includes("link:latest"))!;
    const { slackMarkdown, evidenceRefCount } = render(row);
    expect(evidenceRefCount).toBeGreaterThan(0);
    expect(slackMarkdown).toContain("`[" + evidenceRefCount + "]`");
    expect(slackMarkdown).not.toContain("`[" + (evidenceRefCount + 1) + "]`");
  });
});

describe("without the frontend addresses", () => {
  it("degrades to bare ids rather than to broken links", () => {
    // Both settings are optional in the environment, and a footnote pointing at
    // "/zabbix.php?..." on no host is worse than one that just names its id.
    const row = GOLDEN.find((item) => item.traits.includes("link:latest"))!;
    const rendered = render(row, {}).slackMarkdown;
    expect(rendered).not.toContain("|최근 데이터>");
    expect(rendered).not.toContain("http://");
    expect(rendered).toMatch(/`\[1\]` `/);
  });

  it("drops only the Kibana links when only Kibana is unset", () => {
    const row = GOLDEN.find((item) => item.traits.includes("link:kibana"))!;
    const rendered = render(row, {
      zabbixFrontendUrl: CONFIG.zabbixFrontendUrl,
    }).slackMarkdown;
    expect(rendered).not.toContain("|로그>");
    expect(rendered).toContain("/zabbix");
  });
});
