/**
 * Two ways this service put text somewhere it could not be read as intended.
 *
 * Both were already understood. `escapeSlackText` carries the reason it exists
 * in its own comment, and the Kibana link carries a note about what the query
 * is for. In each case the reasoning was written down and applied in one place
 * out of the several that needed it.
 */

import { describe, expect, it } from "vitest";
import { ackText, clarificationText, type DispatchPayload } from "../src/pipeline.js";
import { formatReport, type FormatConfig } from "../src/report-format.js";

const PAYLOAD: DispatchPayload = {
  request_id: "REQ-20260831-Ev1",
  channel_id: "C-QUESTIONS",
  user_id: "U-ASKER",
  message_ts: "1785900000.000100",
  thread_ts: null,
  question: "payment-service가 왜 멈췄어?",
  received_at: "2026-08-31T02:31:00.000Z",
};

describe("text this service did not write, posted into Slack", () => {
  /**
   * Slack reads `<!channel>` as a broadcast and `<@U…>` as a mention, and the
   * bot repeats a question into the answer channel and a model's words into
   * the question channel. Only the abandonment notice escaped what it carried.
   */
  const BROADCAST = "<!channel> 왜 멈췄어?";

  it("does not let a question broadcast from inside the acknowledgement", () => {
    const text = ackText({ ...PAYLOAD, question: BROADCAST });
    expect(text).not.toContain("<!channel>");
    expect(text).toContain("&lt;!channel&gt;");
  });

  it("escapes the original question of a continuation too", () => {
    const text = ackText({
      ...PAYLOAD,
      parent_request_id: "REQ-20260831-Ev0",
      prior_question: BROADCAST,
      question: "vm-java-docker-2 입니다",
    });
    expect(text).not.toContain("<!channel>");
  });

  it("escapes the supplement a resumed investigation reports", () => {
    const text = ackText({
      ...PAYLOAD,
      parent_request_id: "REQ-20260831-Ev0",
      parent_ack_ts: "1785900000.000001",
      question: BROADCAST,
    });
    expect(text).not.toContain("<!channel>");
  });

  it("does not let a model-written ambiguity broadcast", () => {
    const text = clarificationText(PAYLOAD, {
      ambiguities: ["<!channel> 어느 호스트를 말씀하시는 건가요?"],
      parse_status: "needs_clarification",
    });
    expect(text).not.toContain("<!channel>");
    expect(text).toContain("&lt;!channel&gt;");
  });

  it("still mentions the asker, which this service does write", () => {
    const text = clarificationText(PAYLOAD, {
      ambiguities: ["어느 호스트인가요?"],
      parse_status: "needs_clarification",
    });
    expect(text).toContain("<@U-ASKER>");
  });
});

describe("the Kibana footnote", () => {
  const CONFIG: FormatConfig = {
    kibanaUrl: "http://192.0.2.105:5601",
    kibanaDataViewId: "dv-1",
  };

  function render(searchQuery: string | null) {
    return formatReport(
      {
        request: { request_id: "REQ-1" },
        template: {
          output: { sections: [{ id: "errors", heading: "오류", required: true }] },
        },
        evidencePackage: {
          query_context: { hosts: [{ host: "vm-java-docker-2", host_id: "11094" }] },
          evidence: [
            {
              evidence_id: "log:lines:vm-java-docker-2:abc123",
              window: { from: "2026-08-01T00:00:00Z", to: "2026-08-01T03:00:00Z" },
              resource_ids: { host_id: "11094" },
              search_query: searchQuery,
            },
          ],
        },
        report: {
          title: "테스트",
          sections: [
            {
              id: "errors",
              items: [
                { text: "오류 15건", evidence_refs: ["log:lines:vm-java-docker-2:abc123"] },
              ],
            },
          ],
        },
      },
      CONFIG,
    ).slackMarkdown;
  }

  /** The stored query the reports this formatter was ported from carried. */
  it("keeps a KQL query, which is what the parameter can read", () => {
    const markdown = render('host.name:"vm-java-docker-2" and message:*ERROR*');
    expect(decodeURIComponent(markdown)).toContain("message:*ERROR*");
  });

  /**
   * What the collector stores today. ES|QL in a `language:kuery` parameter
   * opens Discover on a syntax error -- KQL knows no `FROM`, no `|`, no `==`.
   */
  it("does not put an ES|QL statement in a KQL parameter", () => {
    const esql =
      'FROM vm-logs-* | WHERE host.name == "vm-java-docker-2" | STATS n = COUNT(*)';
    const decoded = decodeURIComponent(render(esql));
    expect(decoded).not.toContain("FROM vm-logs-*");
    expect(decoded).toContain('host.name:"vm-java-docker-2"');
  });

  it("does not put a serialised DSL body in a KQL parameter either", () => {
    const decoded = decodeURIComponent(
      render('{"query":{"match":{"message":"ERROR"}}}'),
    );
    expect(decoded).not.toContain('"match"');
    expect(decoded).toContain('host.name:"vm-java-docker-2"');
  });

  it("falls back to the host when the collector carried no query", () => {
    expect(decodeURIComponent(render(null))).toContain('host.name:"vm-java-docker-2"');
  });

  /**
   * Rison quotes with `'` and escapes with `!`, so an apostrophe reaching the
   * value unescaped closes the string early and the whole parameter fails to
   * parse -- the same breakage as the wrong language, through the value.
   */
  it("escapes a quote inside the query rather than ending the parameter", () => {
    const decoded = decodeURIComponent(render("message:*it's broken*"));
    expect(decoded).toContain("!'");
    expect(decoded).not.toContain("query:'message:*it's");
  });
});
