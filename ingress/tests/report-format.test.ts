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

describe("a host Zabbix does not know", () => {
  // Host resolution can now find a name in a log index or an agent list, and
  // neither carries Zabbix's id. Evidence identifies its host by that id, so
  // such a host cannot be looked up -- what matters is that it does not look up
  // as somebody else.
  const row = () => ({
    request_id: "REQ-1",
    user_id: "U000000TEST",
    template_output: { sections: [{ id: "answer", heading: "확인", required: true }] },
    package: {
      evidence: [
        {
          evidence_id: "log:lines:ghost-host:aaaa",
          window: { from: "2026-08-19T00:00:00Z", to: "2026-08-20T00:00:00Z" },
          resource_ids: { host_id: null },
          search_query: null,
        },
      ],
      query_context: {
        hosts: [
          { host: "ghost-host", host_id: null },
          { host: "known-host", host_id: "11094" },
        ],
      },
    },
    report: {
      title: "확인",
      sections: [
        {
          id: "answer",
          body: null,
          items: [
            {
              text: "ghost-host 로그에서 에러 확인",
              label: null,
              evidence_refs: ["log:lines:ghost-host:aaaa"],
              counter_evidence_refs: [],
            },
          ],
        },
      ],
    },
  });

  it("renders without inventing a link", () => {
    const rendered = formatReport(
      {
        request: { request_id: "REQ-1", user_id: "U000000TEST" },
        template: { output: row().template_output as never },
        evidencePackage: row().package as never,
        report: row().report as never,
      },
      CONFIG,
    );
    expect(rendered.slackMarkdown).toContain("ghost-host");
    // No host_id means no "최근 데이터" link; the footnote keeps its bare id.
    expect(rendered.slackMarkdown).not.toContain("|최근 데이터>");
  });

  it("does not borrow another host's name for the log query", () => {
    // The map was keyed by host_id. Keying null would have made every host
    // without one resolve to whichever was stored last, and the footnote would
    // open a search for a machine the quote did not come from.
    const rendered = formatReport(
      {
        request: { request_id: "REQ-1", user_id: "U000000TEST" },
        template: { output: row().template_output as never },
        evidencePackage: row().package as never,
        report: row().report as never,
      },
      CONFIG,
    );
    const link = /_a=([^|\s>]+)/.exec(rendered.slackMarkdown);
    expect(link).not.toBeNull();
    const query = decodeURIComponent(link![1]!);
    expect(query).not.toContain("known-host");
    // No name to scope by, so the search is left open rather than wrong.
    expect(query).toContain("query:'*'");
  });

  it("lists it in the header beside a host that has an id", () => {
    const rendered = formatReport(
      {
        request: { request_id: "REQ-1", user_id: "U000000TEST" },
        template: { output: row().template_output as never },
        evidencePackage: row().package as never,
        report: row().report as never,
      },
      CONFIG,
    );
    expect(rendered.slackMarkdown).toContain("ghost-host, known-host");
  });
});

describe("an item's own tag", () => {
  const item = (label: string | null, text: string) =>
    render({
      request_id: "REQ-TAG",
      user_id: "U1",
      traits: [],
      template_output: {
        sections: [{ id: "summary", heading: "요약", required: true }],
      },
      package: { evidence: [], query_context: {} },
      report: {
        title: "태그",
        sections: [
          {
            id: "summary",
            items: [
              { text, label, evidence_refs: [], counter_evidence_refs: [] },
            ],
          },
        ],
      },
      expected_markdown: "",
    } as never).slackMarkdown ?? "";

  it("renders as the writer spelled it", () => {
    // Uppercasing did nothing to a Korean tag and mangled everything else: a
    // host name came out as MIDIBUS-DOCKER-FTP03_203.0.113.134, which is not
    // the name of that host, and Docker came out as DOCKER.
    expect(item("Docker 관련 프로세스", "docker-proxy")).toContain(
      "*[Docker 관련 프로세스]*",
    );
    expect(item("vm-java-docker-2", "떨어짐")).toContain("*[vm-java-docker-2]*");
  });

  it("is not printed twice when the text repeats it", () => {
    // The writer began carrying a bracketed tag in the text as well, so a line
    // read `[OBSERVED_FAILURE] [변동 구간] 전체 로그 최고치는...` -- the same
    // convention twice, and the louder half wrong.
    const rendered = item("변동 구간", "[변동 구간] 전체 로그 최고치는 7,261건");
    expect(rendered).toContain("*[변동 구간]* 전체 로그 최고치는 7,261건");
    expect(rendered).not.toContain("*[변동 구간]* [변동 구간]");
  });

  it("keeps a bracket the text opens with when there is no label", () => {
    // Then it is the only tag there is, and dropping it would lose it.
    expect(item(null, "[변동 구간] 전체 로그 최고치")).toContain(
      "• [변동 구간] 전체 로그 최고치",
    );
  });
});
