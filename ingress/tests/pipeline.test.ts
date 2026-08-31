/**
 * The workflow's order, kept.
 *
 * Twenty-one n8n nodes became one function, and the risk in that is not a step
 * being wrong but a step being in the wrong place or quietly gone. These pin the
 * order and the destinations: the acknowledgement opens the thread everything
 * else hangs from, answers go to the answer channel while questions back to the
 * asker go to theirs, and the audit trail is written before anything is
 * published.
 */

import { describe, expect, it, vi } from "vitest";
import {
  abandonedText,
  ackText,
  clarificationText,
  runInvestigation,
  type DispatchPayload,
  type PipelineConfig,
} from "../src/pipeline.js";
import type {
  PublishedReport,
  ReportInput,
  RequestRepository,
} from "../src/types.js";

const PAYLOAD: DispatchPayload = {
  request_id: "REQ-1",
  channel_id: "C-QUESTION",
  user_id: "U-ASKER",
  message_ts: "1000.1",
  thread_ts: null,
  question: "어제 에러가 몇 건이었어",
  received_at: "2026-08-20T00:00:00Z",
  parent_request_id: null,
  prior_question: null,
  parent_ack_ts: null,
  slack_ack_ts: null,
};

const CONFIG: PipelineConfig = {
  answerChannelId: "C-ANSWER",
  rcaApiUrl: "http://rca-api:8090",
  internalToken: "internal-token",
  botToken: "xoxb-test",
  rcaTimeoutMs: 900_000,
  slackTimeoutMs: 30_000,
  format: { zabbixFrontendUrl: "http://192.0.2.241/zabbix" },
};

const TEMPLATE_OUTPUT = {
  sections: [{ id: "answer", heading: "확인 결과", required: true }],
};

const REPORT = {
  title: "확인",
  sections: [
    {
      id: "answer",
      items: [{ text: "에러 3건", evidence_refs: [], counter_evidence_refs: [] }],
    },
  ],
};

/**
 * Enough of the repository to run the pipeline, and stateful where the pipeline
 * reads back what it wrote.
 *
 * updateRequestStatus keeps the status it was handed and findRequestStatus
 * answers from it; saveReport keeps its row and findReportByRequest answers
 * from that. One column and one primary key, read back the way the database
 * reads them back. Those pairings are the point: each guard is exactly one of
 * these reads seeing the earlier write, and a fake that answered a constant
 * would let a second run post again and still call it a pass.
 *
 * `seed` stands a run up as a later delivery on its own -- `status` for a
 * request that has already been told something, `published` for one whose
 * report is already out. 'received' is what the insert defaults to, so an
 * unseeded run is a first delivery.
 */
function fakeRepository(
  seed: { status?: string; published?: PublishedReport | null } = {},
) {
  const calls: Record<string, unknown[]> = {
    findRequestStatus: [],
    updateRequestStatus: [],
    recordAgentRun: [],
    saveReport: [],
    listTemplates: [],
    findReportByRequest: [],
  };
  let requestStatus = seed.status ?? "received";
  let report = seed.published ?? null;
  const repository = {
    listTemplates: vi.fn(async (includeDisabled: boolean) => {
      calls.listTemplates!.push(includeDisabled);
      return [{ template_id: "host_state_check" }] as never;
    }),
    findRequestStatus: vi.fn(async (requestId: string) => {
      calls.findRequestStatus!.push(requestId);
      return requestStatus;
    }),
    updateRequestStatus: vi.fn(async (...args: unknown[]) => {
      calls.updateRequestStatus!.push(args);
      requestStatus = String(args[1]);
      return true;
    }),
    recordAgentRun: vi.fn(async (...args: unknown[]) => {
      calls.recordAgentRun!.push(args);
      return true;
    }),
    saveReport: vi.fn(async (...args: unknown[]) => {
      calls.saveReport!.push(args);
      const input = args[1] as ReportInput;
      report = {
        channelId: input.slackChannelId,
        messageTs: input.slackMessageTs ?? null,
      };
      return true;
    }),
    findReportByRequest: vi.fn(async (requestId: string) => {
      calls.findReportByRequest!.push(requestId);
      return report;
    }),
  } as unknown as RequestRepository;
  return { repository, calls };
}

interface SlackPost {
  channel: string;
  text: string;
  thread_ts?: string;
}

/** Slack replies with ascending ts; the RCA reply is whatever the test names. */
function fakeFetch(rca: unknown, options: { rcaStatus?: number } = {}) {
  const slackPosts: SlackPost[] = [];
  const rcaCalls: unknown[] = [];
  let nextTs = 2000;

  const impl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const target = String(url);
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (target.includes("slack.com")) {
      slackPosts.push(body);
      nextTs += 1;
      return new Response(JSON.stringify({ ok: true, ts: String(nextTs) }), {
        headers: { "content-type": "application/json" },
      });
    }
    rcaCalls.push({ url: target, body, headers: init?.headers });
    return new Response(JSON.stringify(rca), {
      status: options.rcaStatus ?? 200,
      headers: { "content-type": "application/json" },
    });
  }) as unknown as typeof fetch;

  return { impl, slackPosts, rcaCalls };
}

const COMPLETED = {
  status: "completed",
  parsed_request: { parse_status: "ready" },
  evidence_package: { evidence: [], query_context: { hosts: [] } },
  report: REPORT,
  template: { output: TEMPLATE_OUTPUT },
  agent_runs: [
    { stage: "question_analyzer", status: "succeeded", model: "m", duration_ms: 10 },
    { stage: "evidence_collector", status: "succeeded", model: "m", duration_ms: 20 },
    { stage: "rca_writer", status: "succeeded", model: "m", duration_ms: 30 },
  ],
};

/**
 * The other thing the RCA service can answer with. One agent run, because it
 * stopped at the question analyzer: there is nothing to collect evidence for
 * until the question is clear.
 */
const NEEDS = {
  status: "needs_clarification",
  parsed_request: { parse_status: "needs_clarification", ambiguities: ["어느 호스트?"] },
  agent_runs: [{ stage: "question_analyzer", status: "succeeded" }],
};

describe("the acknowledgement", () => {
  it("carries the question when it opens its own thread", () => {
    expect(ackText(PAYLOAD)).toContain("• 질문: 어제 에러가 몇 건이었어");
    expect(ackText(PAYLOAD)).toContain("AIOps 조사 접수");
  });

  it("reports only what was added when it lands in the parent thread", () => {
    // The original question is already on screen there.
    const text = ackText({
      ...PAYLOAD,
      parent_request_id: "REQ-0",
      parent_ack_ts: "999.9",
      prior_question: "원래 질문",
    });
    expect(text).toContain("조사 재개");
    expect(text).not.toContain("원래 질문");
  });

  it("carries both questions when a continuation has no parent anchor", () => {
    const text = ackText({
      ...PAYLOAD,
      parent_request_id: "REQ-0",
      prior_question: "원래 질문",
    });
    expect(text).toContain("• 원래 질문: 원래 질문");
    expect(text).toContain("• 보충된 정보: 어제 에러가 몇 건이었어");
  });
});

describe("asking for more information", () => {
  it("lists what was ambiguous and invites a reply", () => {
    const text = clarificationText(PAYLOAD, {
      parse_status: "needs_clarification",
      ambiguities: ["호스트를 특정할 수 없습니다"],
    });
    expect(text).toContain("<@U-ASKER>");
    expect(text).toContain("• 호스트를 특정할 수 없습니다");
    expect(text).toContain("멘션해서 답해주시면");
  });

  it("falls back to naming what is always needed", () => {
    const text = clarificationText(PAYLOAD, { parse_status: "needs_clarification" });
    expect(text).toContain("조사할 호스트와 기준 시각");
  });

  it("does not invite a reply that cannot help", () => {
    // An unsupported request will not become supported by answering.
    const text = clarificationText(PAYLOAD, { parse_status: "unsupported" });
    expect(text).toContain("지원 범위를 벗어난");
    expect(text).not.toContain("멘션해서 답해주시면");
  });
});

describe("giving up on a question", () => {
  it("names the asker, the request and what went wrong", () => {
    const text = abandonedText(PAYLOAD, "RCA service returned HTTP 500");

    expect(text).toContain("<@U-ASKER>");
    expect(text).toContain("REQ-1");
    expect(text).toContain("RCA service returned HTTP 500");
    expect(text).toContain("조사를 완료하지 못했습니다");
  });

  it("keeps the error to a message's length", () => {
    // A stack trace pasted into Slack helps nobody it is addressed to.
    const text = abandonedText(PAYLOAD, "x".repeat(5_000));

    expect(text.length).toBeLessThan(600);
  });

  it("points the way back at the channel that accepts questions", () => {
    // This is usually read in the answer channel, under the acknowledgement,
    // and app.ts opens an investigation only for the question channel. Saying
    // "ask again" without saying where left the asker following an
    // instruction that is silently ignored wherever they were standing.
    const text = abandonedText(PAYLOAD, "boom");

    expect(text).toContain("<#C-QUESTION>");
  });

  it("defuses Slack markup carried in from the error", () => {
    // The stage in this reason comes from the RCA service's own response.
    // Unescaped, a failure notice pages the channel it is posted in.
    const text = abandonedText(
      PAYLOAD,
      "RCA returned an unknown agent stage: <!channel>",
    );

    expect(text).not.toContain("<!channel>");
    expect(text).toContain("&lt;!channel&gt;");
  });

  it("cannot be trimmed into a reopened bracket", () => {
    // Escaped before it is cut, so no slice can end mid-entity and leave a
    // bare "<" for Slack to read as the start of markup.
    const text = abandonedText(PAYLOAD, "<".repeat(5_000));

    expect(text).not.toContain("<!");
    // The only angle brackets left are the two this file writes itself: the
    // mention of the asker and the link to the question channel.
    expect(text.match(/</g)).toHaveLength(2);
  });
});

describe("a completed investigation", () => {
  async function run(payload = PAYLOAD) {
    const { repository, calls } = fakeRepository();
    const { impl, slackPosts, rcaCalls } = fakeFetch(COMPLETED);
    await runInvestigation(payload, { repository, fetchImpl: impl }, CONFIG);
    return { calls, slackPosts, rcaCalls, repository };
  }

  it("acknowledges in the answer channel before anything else", async () => {
    const { slackPosts } = await run();
    expect(slackPosts[0]!.channel).toBe("C-ANSWER");
    expect(slackPosts[0]!.text).toContain("조사 접수");
    expect(slackPosts[0]!.thread_ts).toBeUndefined();
  });

  it("anchors the report to the acknowledgement's thread", async () => {
    const { slackPosts } = await run();
    expect(slackPosts).toHaveLength(2);
    expect(slackPosts[1]!.thread_ts).toBe("2001");
    expect(slackPosts[1]!.channel).toBe("C-ANSWER");
  });

  it("records the anchor with the status so replies can find it", async () => {
    const { calls } = await run();
    expect(calls.updateRequestStatus![0]).toEqual([
      "REQ-1",
      "analyzing_question",
      undefined,
      "2001",
    ]);
  });

  it("asks the RCA service for an investigation over the enabled templates", async () => {
    const { rcaCalls, calls } = await run();
    expect(calls.listTemplates).toEqual([false]);
    const call = rcaCalls[0] as { url: string; body: Record<string, unknown> };
    expect(call.url).toBe("http://rca-api:8090/v1/investigations");
    expect(call.body.templates).toEqual([{ template_id: "host_state_check" }]);
    expect((call.body.request as Record<string, unknown>).question).toBe(
      "어제 에러가 몇 건이었어",
    );
  });

  it("writes every audit record before publishing anything", async () => {
    // A report published without its agent runs is a report nobody can account
    // for afterwards.
    const { calls } = await run();
    expect(calls.recordAgentRun).toHaveLength(3);
  });

  it("stores the report against the message it was published as", async () => {
    const { calls } = await run();
    const [requestId, input] = calls.saveReport![0] as [string, Record<string, unknown>];
    expect(requestId).toBe("REQ-1");
    expect(input.slackChannelId).toBe("C-ANSWER");
    expect(input.slackMessageTs).toBe("2002");
    expect(String(input.slackMarkdown)).toContain("확인 결과");
  });

  it("does not acknowledge a second time when a retry runs it again", async () => {
      // Delivery is retried: a failing investigation goes back to the queue and
      // runs again. Posting unconditionally put three identical
      // acknowledgements in the channel for one question.
      const { repository } = fakeRepository();
      const { impl, slackPosts } = fakeFetch(COMPLETED);
      await runInvestigation(
        { ...PAYLOAD, slack_ack_ts: "1700.1" },
        { repository, fetchImpl: impl },
        CONFIG,
      );
      expect(slackPosts).toHaveLength(1);
      expect(slackPosts[0]!.text).not.toContain("조사 접수");
      // And the report still lands in the thread the first attempt opened.
      expect(slackPosts[0]!.thread_ts).toBe("1700.1");
    });

    it("anchors a continuation to the parent rather than to its own ack", async () => {
    const { slackPosts } = await run({
      ...PAYLOAD,
      parent_request_id: "REQ-0",
      parent_ack_ts: "111.1",
    });
    expect(slackPosts[0]!.thread_ts).toBe("111.1");
    expect(slackPosts[1]!.thread_ts).toBe("111.1");
  });
});

describe("an investigation delivered a second time", () => {
  /**
   * What the queue does when a delivery succeeds and cannot be closed: the same
   * request, claimed and run again against the rows the first run left behind.
   * The second payload carries the anchor the first run recorded, because
   * claimDispatch reads it back onto the job.
   */
  async function runTwice() {
    const { repository, calls } = fakeRepository();
    const { impl, slackPosts, rcaCalls } = fakeFetch(COMPLETED);
    await runInvestigation(PAYLOAD, { repository, fetchImpl: impl }, CONFIG);
    await runInvestigation(
      { ...PAYLOAD, slack_ack_ts: "2001" },
      { repository, fetchImpl: impl },
      CONFIG,
    );
    return { calls, slackPosts, rcaCalls };
  }

  it("does not post a second report for a question already answered", async () => {
    // completeDispatch failing after a delivery succeeded leaves the row for
    // the claim lock to lapse, deliberately: a failure to close the row must
    // not be read as a failed investigation. The job is claimed again as a
    // result, and without a guard the asker read the same answer twice in one
    // thread while the upsert behind it showed a single tidy row.
    const { slackPosts } = await runTwice();

    // The acknowledgement and the report from the first run, and nothing since.
    expect(slackPosts).toHaveLength(2);
    const reports = slackPosts.filter((post) => post.text.includes("확인 결과"));
    expect(reports).toHaveLength(1);
  });

  it("asks the RCA service nothing it has already answered", async () => {
    // The guard is read before the acknowledgement rather than beside the post,
    // so a redelivery costs one query -- not a fresh investigation whose result
    // is thrown away, and not a second set of audit records for one of them.
    const { rcaCalls, calls } = await runTwice();

    expect(rcaCalls).toHaveLength(1);
    expect(calls.recordAgentRun).toHaveLength(3);
    expect(calls.saveReport).toHaveLength(1);
  });

  it("asks about the request it was handed, on every attempt", async () => {
    const { calls } = await runTwice();
    expect(calls.findReportByRequest).toEqual(["REQ-1", "REQ-1"]);
  });

  it("counts a report row with no message ts as published", async () => {
    // saveReport stores the row whether or not Slack answered with a ts, and it
    // stores it only after the post returned. The row is the marker; a missing
    // ts says something about the Slack response and nothing about whether the
    // report is in the thread.
    const { repository, calls } = fakeRepository({
      published: { channelId: "C-ANSWER", messageTs: null },
    });
    const { impl, slackPosts } = fakeFetch(COMPLETED);

    await runInvestigation(
      { ...PAYLOAD, slack_ack_ts: "2001" },
      { repository, fetchImpl: impl },
      CONFIG,
    );

    expect(slackPosts).toHaveLength(0);
    expect(calls.saveReport).toHaveLength(0);
  });

  it("still publishes when the attempt before it died short of the report", async () => {
    // The guard keys on the report, not on the acknowledgement. A retry of an
    // investigation that failed midway has a thread already open and an answer
    // still owed, and refusing to publish that one would be the silence this
    // queue exists to prevent.
    const { repository, calls } = fakeRepository();
    const { impl, slackPosts } = fakeFetch(COMPLETED);

    await runInvestigation(
      { ...PAYLOAD, slack_ack_ts: "1700.1" },
      { repository, fetchImpl: impl },
      CONFIG,
    );

    expect(slackPosts).toHaveLength(1);
    expect(slackPosts[0]!.text).toContain("확인 결과");
    expect(slackPosts[0]!.thread_ts).toBe("1700.1");
    expect(calls.saveReport).toHaveLength(1);
  });
});

describe("an investigation that needs more information", () => {
  it("asks in the channel the question came from", async () => {
    const { repository } = fakeRepository();
    const { impl, slackPosts } = fakeFetch(NEEDS);
    await runInvestigation(PAYLOAD, { repository, fetchImpl: impl }, CONFIG);
    expect(slackPosts[1]!.channel).toBe("C-QUESTION");
    expect(slackPosts[1]!.thread_ts).toBe("1000.1");
    expect(slackPosts[1]!.text).toContain("어느 호스트?");
  });

  it("records the status and publishes no report", async () => {
    const { repository, calls } = fakeRepository();
    const { impl } = fakeFetch(NEEDS);
    await runInvestigation(PAYLOAD, { repository, fetchImpl: impl }, CONFIG);
    expect(calls.saveReport).toHaveLength(0);
    expect(calls.updateRequestStatus![1]).toEqual(["REQ-1", "needs_clarification"]);
  });
});

describe("a clarification delivered a second time", () => {
  /**
   * What the queue does when a delivery succeeds and the row holding it cannot
   * be closed: the same request, claimed again once the lock lapses, run
   * against what the first delivery left behind. The second payload carries the
   * anchor the first run recorded, because claimDispatch reads it back onto the
   * job.
   */
  async function runTwice(rca: unknown = NEEDS) {
    const { repository, calls } = fakeRepository();
    const { impl, slackPosts, rcaCalls } = fakeFetch(rca);
    await runInvestigation(PAYLOAD, { repository, fetchImpl: impl }, CONFIG);
    await runInvestigation(
      { ...PAYLOAD, slack_ack_ts: "2001" },
      { repository, fetchImpl: impl },
      CONFIG,
    );
    return { calls, slackPosts, rcaCalls };
  }

  it("does not ask the asker the same question twice", async () => {
    // completeDispatch failing after a delivery succeeded leaves the row for
    // the claim lock to lapse, deliberately: a failure to close a row must not
    // be read as a failed investigation. The job is claimed again as a result,
    // and without a guard the asker was mentioned a second time by the same
    // question. The acknowledgement survives that by reading back the ts the
    // first attempt recorded; the clarification had nothing to read.
    const { slackPosts } = await runTwice();

    // The acknowledgement and the question from the first run, and nothing
    // since.
    expect(slackPosts).toHaveLength(2);
    const asked = slackPosts.filter((post) => post.text.includes("어느 호스트?"));
    expect(asked).toHaveLength(1);
  });

  it("asks the RCA service nothing it has already answered", async () => {
    // The status is read at the top rather than beside the post, so a
    // redelivery costs one query -- not a second investigation whose answer is
    // thrown away, and not a second set of audit records for it.
    const { rcaCalls, calls } = await runTwice();

    expect(rcaCalls).toHaveLength(1);
    expect(calls.recordAgentRun).toHaveLength(1);
  });

  it("leaves the status as the delivery that asked the question left it", async () => {
    // The second run returns before the acknowledgement step, so it never
    // writes analyzing_question over the marker. That is what keeps a third
    // delivery from finding a request that looks unasked again.
    const { calls } = await runTwice();

    expect(calls.updateRequestStatus).toEqual([
      ["REQ-1", "analyzing_question", undefined, "2001"],
      ["REQ-1", "needs_clarification"],
    ]);
  });

  it("asks about the request it was handed, on every attempt", async () => {
    const { calls } = await runTwice();

    expect(calls.findRequestStatus).toEqual(["REQ-1", "REQ-1"]);
  });

  it("does not repeat a verdict that the question cannot be investigated", async () => {
    // unsupported is the branch's other outcome -- the same post without the
    // invitation to reply -- and it is stored as the status the same way. The
    // asker has been told, so a redelivery has nothing left to tell them.
    const { slackPosts } = await runTwice({
      ...NEEDS,
      status: "unsupported",
      parsed_request: { parse_status: "unsupported" },
    });

    expect(slackPosts).toHaveLength(2);
    const asked = slackPosts.filter((post) => post.text.includes("지원 범위를 벗어난"));
    expect(asked).toHaveLength(1);
  });

  it("still asks when the attempt before it stopped short of the question", async () => {
    // analyzing_question is what the acknowledgement step writes on its way
    // past, so every attempt leaves it behind whether it reached an answer or
    // died at the RCA call. Reading it as "already asked" would strand the asker in silence,
    // and it is why the marker is read before that write rather than inside the
    // branch that posts, where it could only ever see this.
    const { repository, calls } = fakeRepository({ status: "analyzing_question" });
    const { impl, slackPosts } = fakeFetch(NEEDS);

    await runInvestigation(
      { ...PAYLOAD, slack_ack_ts: "1700.1" },
      { repository, fetchImpl: impl },
      CONFIG,
    );

    expect(slackPosts).toHaveLength(1);
    expect(slackPosts[0]!.channel).toBe("C-QUESTION");
    expect(slackPosts[0]!.text).toContain("어느 호스트?");
    expect(calls.updateRequestStatus![1]).toEqual(["REQ-1", "needs_clarification"]);
  });

  it("does not put a question back to an asker who has already answered it", async () => {
    // saveSlackRequest moves the parent to clarified when the reply lands, so a
    // stale delivery of that parent finds this rather than the status it wrote
    // itself. Asking again from here is the worse half of the same bug: the
    // question is not merely repeated, it has been answered already.
    const { repository, calls } = fakeRepository({ status: "clarified" });
    const { impl, slackPosts } = fakeFetch(NEEDS);

    await runInvestigation(
      { ...PAYLOAD, slack_ack_ts: "2001" },
      { repository, fetchImpl: impl },
      CONFIG,
    );

    expect(slackPosts).toHaveLength(0);
    expect(calls.updateRequestStatus).toHaveLength(0);
  });
});

describe("failures the queue should retry", () => {
  it("throws when the RCA service refuses", async () => {
    const { repository } = fakeRepository();
    const { impl } = fakeFetch({}, { rcaStatus: 503 });
    await expect(
      runInvestigation(PAYLOAD, { repository, fetchImpl: impl }, CONFIG),
    ).rejects.toThrow(/HTTP 503/);
  });

  it("throws rather than publishing an investigation with no audit trail", async () => {
    const { repository } = fakeRepository();
    const { impl } = fakeFetch({ ...COMPLETED, agent_runs: [] });
    await expect(
      runInvestigation(PAYLOAD, { repository, fetchImpl: impl }, CONFIG),
    ).rejects.toThrow(/no agent audit records/);
  });

  it("throws on a stage the audit table has no column for", async () => {
    // Dropping it silently would leave a gap nobody could see afterwards.
    const { repository } = fakeRepository();
    const { impl } = fakeFetch({
      ...COMPLETED,
      agent_runs: [{ stage: "daydreaming", status: "succeeded" }],
    });
    await expect(
      runInvestigation(PAYLOAD, { repository, fetchImpl: impl }, CONFIG),
    ).rejects.toThrow(/unknown agent stage/);
  });
});
