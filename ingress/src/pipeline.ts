/**
 * One Slack question, from acknowledgement to posted report.
 *
 * This is the n8n workflow, in the service that already owns the Slack side.
 * The workflow held twenty-one nodes and none of them reasoned: seven were HTTP
 * calls back to this process's own `/internal/*` routes, three were Slack posts,
 * one called the RCA service, and the rest formatted and asserted. Everything
 * addressed over HTTP here is a method call.
 *
 * The order is the workflow's order, and the reasons are the workflow's reasons.
 * What changes is that a step failing throws where the caller can see it, rather
 * than ending an execution that cannot report its own failure -- the one class
 * of outage in this stack that left a question acknowledged and then silent.
 */

import { formatReport, type FormatConfig } from "./report-format.js";
import { CLARIFICATION_ASKED } from "./request-status.js";
import { postMessage } from "./slack.js";
import type { AgentRunInput, RequestRepository } from "./types.js";

/** The row the dispatcher claimed, as the queue stores it. */
export interface DispatchPayload {
  request_id: string;
  channel_id: string;
  user_id: string;
  message_ts: string;
  thread_ts?: string | null;
  question: string;
  received_at: string;
  parent_request_id?: string | null;
  prior_question?: string | null;
  parent_ack_ts?: string | null;
  /** Set once this request has been acknowledged, by whichever attempt did it. */
  slack_ack_ts?: string | null;
}

export interface PipelineConfig {
  /** Where answers are published. Not the channel the question came from. */
  answerChannelId: string;
  rcaApiUrl: string;
  internalToken: string;
  botToken: string;
  /** The investigation's own ceiling; the workflow allowed 900 seconds. */
  rcaTimeoutMs: number;
  slackTimeoutMs: number;
  format: FormatConfig;
}

export interface PipelineDeps {
  repository: RequestRepository;
  fetchImpl?: typeof fetch;
}

interface RcaResponse {
  status: string;
  parsed_request?: { ambiguities?: string[]; parse_status?: string };
  evidence_package?: unknown;
  report?: unknown;
  template?: { output?: unknown };
  agent_runs?: AgentRunLike[];
}

interface AgentRunLike {
  stage?: string;
  status?: string;
  model?: string;
  duration_ms?: number;
  output?: unknown;
  error?: string;
}

/**
 * The acknowledgement, which is also what opens the thread.
 *
 * A continuation lands inside the parent's thread, where the original question
 * is already on screen, so it only needs to report what was added. Without a
 * parent anchor it starts its own thread and must carry the question itself.
 */
export function ackText(payload: DispatchPayload): string {
  const continues = Boolean(payload.parent_request_id);

  if (continues && payload.parent_ack_ts) {
    return [
      "🔎 *조사 재개* — 보충된 정보로 이어서 조사합니다.",
      "• 요청 ID: `" + payload.request_id + "`",
      "• 보충된 정보: " + payload.question,
    ].join("\n");
  }

  const lines = ["🔎 *AIOps 조사 접수*", "• 요청 ID: `" + payload.request_id + "`"];
  if (continues && payload.prior_question) {
    lines.push("• 원래 질문: " + payload.prior_question);
    lines.push("• 보충된 정보: " + payload.question);
  } else {
    lines.push("• 질문: " + payload.question);
  }
  return lines.join("\n");
}

/**
 * What to say when the question cannot be investigated as asked.
 *
 * Goes to the channel the question came from rather than to the answer channel:
 * it is addressed to the asker and is waiting on their reply.
 */
export function clarificationText(
  payload: DispatchPayload,
  parsed: RcaResponse["parsed_request"],
): string {
  const ambiguities = Array.isArray(parsed?.ambiguities) ? parsed.ambiguities : [];
  const unsupported = parsed?.parse_status === "unsupported";

  const lines =
    ambiguities.length > 0
      ? ambiguities.map((item) => "• " + item)
      : ["• 조사할 호스트와 기준 시각을 알려주세요."];

  // Ping the asker. Slack notifies thread participants only weakly, and this
  // message is a question addressed to them.
  const mention = payload.user_id ? "<@" + payload.user_id + "> " : "";
  const header = unsupported
    ? "⛔ " + mention + "*지원 범위를 벗어난 요청입니다*"
    : "❓ " + mention + "*조사에 필요한 정보가 더 있습니다*";

  const sections = [header, "• 요청 ID: `" + payload.request_id + "`", lines.join("\n")];

  // Only invite a reply when one would actually help. An unsupported request
  // will not become supported by answering.
  if (!unsupported) {
    sections.push(
      "_이 스레드에 저를 멘션해서 답해주시면 원래 질문과 함께 이어서 조사합니다._",
    );
  }
  return sections.join("\n\n");
}

/**
 * What to say when the queue has given up on a question.
 *
 * The caller posts this under the acknowledgement when there is one: that
 * thread is the investigation, and it is the thread left waiting on a report
 * that is not coming. A request that died before it was ever acknowledged has
 * no such thread, so it falls back to the channel it was asked in.
 *
 * The last error goes in because the asker is the one deciding what to do next,
 * and an RCA service returning HTTP 500 and an MCP call timing out call for
 * different things. Trimmed, because this is a message and not a log line.
 *
 * The way back names the question channel rather than saying "again". This is
 * usually read in the answer channel, under the acknowledgement, and a mention
 * there is not a question: app.ts only opens an investigation for the question
 * channel, and a threaded mention anywhere else is taken for a note on a
 * report -- of which an abandoned request has none, so it is dropped. An
 * instruction the asker can follow and still be ignored is the silence this
 * message exists to break, one step further along.
 */
export function abandonedText(payload: DispatchPayload, reason: string): string {
  // Ping the asker. They are being told their question is dead, and a thread
  // they may have stopped watching is not enough.
  const mention = payload.user_id ? "<@" + payload.user_id + "> " : "";

  return [
    "🛑 " + mention + "*조사를 완료하지 못했습니다*",
    "• 요청 ID: `" + payload.request_id + "`",
    "• 마지막 오류: " + escapeSlackText(reason).slice(0, 300),
    "",
    "_<#" +
      payload.channel_id +
      "> 채널에서 같은 질문을 다시 멘션하시면 새로 조사합니다._",
  ].join("\n");
}

/**
 * Neutralise Slack's markup in text this service did not write.
 *
 * The reason carried here is an error string, and part of it comes from the
 * RCA service's own response -- toAgentRun interpolates the stage it was sent.
 * Slack reads `<!channel>` as a broadcast and `<@U…>` as a mention, so an
 * unescaped error could page a channel from inside a failure notice. Escaped
 * before it is trimmed, so a cut can never land mid-entity and re-open a
 * bracket.
 */
function escapeSlackText(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const KNOWN_STAGES = new Set(["question_analyzer", "evidence_collector", "rca_writer"]);

/** Only the stages the audit table knows; anything else is dropped, loudly. */
function toAgentRun(run: AgentRunLike): AgentRunInput {
  if (!KNOWN_STAGES.has(String(run.stage))) {
    throw new Error(`RCA returned an unknown agent stage: ${String(run.stage)}`);
  }
  return {
    stage: run.stage as AgentRunInput["stage"],
    status: run.status === "failed" ? "failed" : "succeeded",
    ...(run.model ? { model: run.model } : {}),
    ...(typeof run.duration_ms === "number" ? { durationMs: run.duration_ms } : {}),
    ...(run.output === undefined ? {} : { output: run.output }),
    ...(run.error ? { error: run.error } : {}),
  };
}

export async function runInvestigation(
  payload: DispatchPayload,
  deps: PipelineDeps,
  config: PipelineConfig,
): Promise<void> {
  const { repository } = deps;
  const send = deps.fetchImpl ?? fetch;

  // 0. Whether an earlier delivery already finished with this request.
  //
  //    The queue is at-least-once and means it. completeDispatch runs as a
  //    step of its own after the delivery returns, and a failure there is
  //    deliberately not treated as a failed delivery -- doing that retried an
  //    investigation that had already had its say and, on the last attempt,
  //    told the asker their answered question had failed. The row is left for
  //    the claim lock to lapse instead, which means the job is claimed again
  //    and this function runs a second time for a request that is finished.
  //
  //    The acknowledgement below already survives that by reading back the ts
  //    the first attempt recorded. Neither of the two things this function can
  //    post afterwards did. They end in different places and are marked in
  //    different places, so they are read separately -- but both are read
  //    here rather than beside their posts, because nothing in between is
  //    worth repeating either: a redelivery costs two indexed reads instead of
  //    a second acknowledgement thread, a second RCA run and a second set of
  //    audit records. Returning normally is what closes the job -- the caller
  //    marks a delivery that did not throw as complete, so this attempt
  //    finishes the bookkeeping the last one could not.
  //
  //    At most one of the two can fire. saveReport writes 'completed' in the
  //    same transaction as the report row, and 'completed' is not one of the
  //    statuses that mean a clarification was asked, so the order of the two
  //    reads is free.

  //    A clarification is marked by the request's own status, because it
  //    writes no row of its own for a repeat to be absorbed into: the same
  //    question went to the asker twice, mentioning them again. Read rather
  //    than carried on the payload -- claimDispatch could return it the way it
  //    returns slack_ack_ts, but that value would be a snapshot taken at the
  //    claim, and the write that records the anchor below lays
  //    analyzing_question over the stored status on every attempt, so the
  //    delivery would be deciding on something it invalidates itself.
  //    server.ts casts the queue's payload into this shape besides, so a field
  //    that went missing would quietly stop guarding anything rather than fail
  //    to compile.
  const status = await repository.findRequestStatus(payload.request_id);
  if (status !== null && CLARIFICATION_ASKED.includes(status)) {
    return;
  }

  //    A report is marked by the report row, which means what the guard needs
  //    it to mean: saveReport is the last thing this function does, so a row
  //    exists only if the post it describes went out. Without this read,
  //    saveReport being an upsert meant the stored row absorbed the repeat
  //    silently while a second report landed in the thread under the first.
  const published = await repository.findReportByRequest(payload.request_id);
  if (published) {
    return;
  }

  // 1. Acknowledge, and find the thread everything else hangs from. A
  //    continuation replies under the parent's acknowledgement so one
  //    investigation stays one thread.
  //
  //    Only once. Delivery is retried -- a failing investigation is handed back
  //    to the queue and run again -- and posting here unconditionally put three
  //    identical acknowledgements in the channel for one question. The
  //    timestamp of the first one is on the request, so a retry rejoins that
  //    thread instead of starting another.
  let anchor = payload.slack_ack_ts ?? null;
  if (!anchor) {
    const posted = await postMessage({
      botToken: config.botToken,
      channel: config.answerChannelId,
      text: ackText(payload),
      threadTs: payload.parent_ack_ts ?? undefined,
      timeoutMs: config.slackTimeoutMs,
      fetchImpl: send,
    });
    anchor = payload.parent_ack_ts || posted.ts;
  }
  if (!anchor) {
    throw new Error("Slack acknowledgement returned no ts to anchor this investigation to");
  }

  await repository.updateRequestStatus(
    payload.request_id,
    "analyzing_question",
    undefined,
    anchor,
  );

  // 2. The catalog the question analyzer classifies against. Disabled templates
  //    are excluded, which is how a retired report kind leaves circulation.
  const templates = await repository.listTemplates(false);

  const response = await send(`${config.rcaApiUrl}/v1/investigations`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-aiops-internal-token": config.internalToken,
    },
    body: JSON.stringify({
      request: {
        request_id: payload.request_id,
        source: "slack",
        received_at: payload.received_at,
        timezone: "Asia/Seoul",
        question: payload.question,
        metadata: {
          channel_id: payload.channel_id,
          user_id: payload.user_id,
          message_ts: payload.message_ts,
          thread_ts: payload.thread_ts || null,
          parent_request_id: payload.parent_request_id || null,
        },
      },
      prior_question: payload.prior_question || null,
      templates,
    }),
    signal: AbortSignal.timeout(config.rcaTimeoutMs),
  });
  if (!response.ok) {
    throw new Error(`RCA service returned HTTP ${response.status}`);
  }
  const result = (await response.json()) as RcaResponse;

  // 3. The audit trail, before anything is posted. A report published without
  //    its agent runs recorded is a report nobody can account for afterwards.
  const runs = Array.isArray(result.agent_runs) ? result.agent_runs : [];
  if (runs.length === 0) {
    throw new Error("RCA service returned no agent audit records");
  }
  for (const run of runs) {
    await repository.recordAgentRun(payload.request_id, toAgentRun(run));
  }

  if (result.status !== "completed") {
    const text = clarificationText(payload, result.parsed_request);
    await postMessage({
      botToken: config.botToken,
      channel: payload.channel_id,
      text,
      threadTs: payload.thread_ts || payload.message_ts,
      timeoutMs: config.slackTimeoutMs,
      fetchImpl: send,
    });
    // Recorded after the post, and that order is the load-bearing half of the
    // guard at the top. Written first, the status would claim a question the
    // post after it might never make, and the next delivery would read that
    // claim and skip -- leaving the asker waiting on a clarification nobody
    // ever sent them, which is the silence this queue exists to prevent. A
    // post that lands and then fails to record is retried and does ask twice.
    // Between asking twice and never asking, this is the direction to be wrong
    // in.
    await repository.updateRequestStatus(payload.request_id, result.status);
    return;
  }

  const { slackMarkdown } = formatReport(
    {
      request: { request_id: payload.request_id, user_id: payload.user_id },
      template: { output: (result.template?.output ?? null) as never },
      evidencePackage: (result.evidence_package ?? {}) as never,
      report: (result.report ?? {}) as never,
    },
    config.format,
  );

  const report = await postMessage({
    botToken: config.botToken,
    channel: config.answerChannelId,
    text: slackMarkdown,
    threadTs: anchor,
    timeoutMs: config.slackTimeoutMs,
    fetchImpl: send,
  });

  // Saved after posting so the stored row carries the message it was published
  // as, which is what a reaction on that message is later matched against.
  //
  // The order is also what makes this row safe to read as "already published"
  // at the top. Written first, it would claim a publication that the post
  // after it might never make, and the next delivery would read that claim and
  // skip -- leaving the question acknowledged and never answered. A post that
  // lands and then fails to save is retried and does duplicate; between a
  // duplicate report and a silent one, this is the direction to be wrong in.
  await repository.saveReport(payload.request_id, {
    parsedRequest: result.parsed_request,
    evidencePackage: result.evidence_package,
    rcaReport: result.report,
    slackMarkdown,
    slackChannelId: config.answerChannelId,
    ...(report.ts ? { slackMessageTs: report.ts } : {}),
  });
}
