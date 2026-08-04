import request from "supertest";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createApp } from "../src/app.js";
import type { AppConfig } from "../src/config.js";
import { createSlackSignature } from "../src/slack.js";
import type {
  AcceptedSlackRequest,
  AgentRunInput,
  DispatchJob,
  FeedbackLabel,
  PendingClarification,
  ReportFeedbackInput,
  ReportInput,
  ReportNoteInput,
  ReportRef,
  RequestRepository,
  SaveFeedbackResult,
  SaveRequestResult,
  SystemErrorInput,
} from "../src/types.js";

class FakeRepository implements RequestRepository {
  saveResult: SaveRequestResult = { created: true, requestId: "REQ-TEST" };
  pendingClarification: PendingClarification | null = null;
  savedRequests: AcceptedSlackRequest[] = [];
  report: ReportRef | null = { requestId: "REQ-REPORT", threadTs: "1700000000.100" };
  feedbackResult: SaveFeedbackResult = { created: true, shouldAskForCorrection: false };
  saveSlackRequest = vi.fn(async (input: AcceptedSlackRequest) => {
    this.savedRequests.push(input);
    return this.saveResult;
  });
  findPendingClarification = vi.fn(
    async (): Promise<PendingClarification | null> => this.pendingClarification,
  );
  ping = vi.fn(async () => undefined);
  claimDispatch = vi.fn(async (): Promise<DispatchJob | null> => null);
  completeDispatch = vi.fn(async () => undefined);
  retryDispatch = vi.fn(async () => undefined);
  updateRequestStatus = vi.fn(async () => true);
  recordAgentRun = vi.fn(async (_id: string, _input: AgentRunInput) => true);
  saveReport = vi.fn(async (_id: string, _input: ReportInput) => true);
  recordSystemError = vi.fn(async (_input: SystemErrorInput) => undefined);
  getRequest = vi.fn(async () => ({ request_id: "REQ-TEST" }));
  findReportByMessage = vi.fn(async (): Promise<ReportRef | null> => this.report);
  findReportByThread = vi.fn(async (): Promise<ReportRef | null> => this.report);
  saveReportFeedback = vi.fn(
    async (_input: ReportFeedbackInput): Promise<SaveFeedbackResult> =>
      this.feedbackResult,
  );
  removeReportFeedback = vi.fn(async () => true);
  saveReportNote = vi.fn(async (_input: ReportNoteInput) => true);
}

const signingSecret = "test-signing-secret";
const internalToken = "internal-token-with-at-least-24-characters";
const config: AppConfig = {
  port: 8080,
  databaseUrl: "postgres://unused",
  slackSigningSecret: signingSecret,
  slackQuestionChannelId: "C-QUESTIONS",
  slackBotUserId: "U-BOT",
  slackAllowedUserIds: new Set<string>(),
  internalToken,
  n8nWebhookUrl: "http://n8n:5678/webhook/aiops-process",
  dispatchIntervalMs: 1000,
  dispatchTimeoutMs: 10000,
  labelReactions: new Map<string, FeedbackLabel>([
    ["white_check_mark", "correct"],
    ["x", "incorrect"],
    ["thinking_face", "partial"],
  ]),
};

function signedHeaders(rawBody: string): Record<string, string> {
  const timestamp = Math.floor(Date.now() / 1_000).toString();
  return {
    "content-type": "application/json",
    "x-slack-request-timestamp": timestamp,
    "x-slack-signature": createSlackSignature(signingSecret, timestamp, rawBody),
  };
}

describe("Slack ingress", () => {
  let repository: FakeRepository;
  const wake = vi.fn(() => undefined);

  beforeEach(() => {
    repository = new FakeRepository();
    wake.mockReset();
  });

  it("handles Slack URL verification after signature validation", async () => {
    const rawBody = JSON.stringify({
      type: "url_verification",
      challenge: "challenge-value",
    });
    const response = await request(createApp({ config, repository }))
      .post("/slack/events")
      .set(signedHeaders(rawBody))
      .send(rawBody);

    expect(response.status).toBe(200);
    expect(response.body).toEqual({ challenge: "challenge-value" });
  });

  it("persists a valid event before acknowledging it and wakes the dispatcher", async () => {
    const rawBody = JSON.stringify({
      type: "event_callback",
      event_id: "Ev123",
      event_time: 1_700_000_000,
      team_id: "T1",
      event: {
        type: "app_mention",
        channel: "C-QUESTIONS",
        user: "U1",
        text: "<@U-BOT> web-01 CPU 장애 조사",
        ts: "1700000000.123",
      },
    });
    const response = await request(
      createApp({ config, repository, onRequestAccepted: wake }),
    )
      .post("/slack/events")
      .set(signedHeaders(rawBody))
      .send(rawBody);

    expect(response.status).toBe(200);
    expect(response.body).toEqual({
      accepted: true,
      duplicate: false,
      request_id: "REQ-TEST",
    });
    expect(repository.saveSlackRequest).toHaveBeenCalledWith(
      expect.objectContaining({
        requestId: "REQ-20231115-Ev123",
        question: "web-01 CPU 장애 조사",
        channelId: "C-QUESTIONS",
      }),
    );
    expect(wake).toHaveBeenCalledOnce();
  });

  it("links a threaded reply to the request that asked for clarification", async () => {
    repository.pendingClarification = {
      requestId: "REQ-PARENT",
      question: "CPU 상태 괜찮아?",
    };
    const rawBody = JSON.stringify({
      type: "event_callback",
      event_id: "Ev456",
      event_time: 1_700_000_100,
      event: {
        type: "app_mention",
        channel: "C-QUESTIONS",
        user: "U1",
        text: "<@U-BOT> web-01 이야",
        ts: "1700000100.999",
        thread_ts: "1700000000.123",
      },
    });
    const response = await request(
      createApp({ config, repository, onRequestAccepted: wake }),
    )
      .post("/slack/events")
      .set(signedHeaders(rawBody))
      .send(rawBody);

    expect(response.status).toBe(200);
    expect(response.body.clarifies).toBe("REQ-PARENT");
    expect(repository.findPendingClarification).toHaveBeenCalledWith(
      "C-QUESTIONS",
      "1700000000.123",
    );
    expect(repository.savedRequests[0]).toMatchObject({
      parentRequestId: "REQ-PARENT",
      question: "web-01 이야",
    });
  });

  it("treats a threaded reply with no pending clarification as a new request", async () => {
    repository.pendingClarification = null;
    const rawBody = JSON.stringify({
      type: "event_callback",
      event_id: "Ev789",
      event_time: 1_700_000_200,
      event: {
        type: "app_mention",
        channel: "C-QUESTIONS",
        user: "U1",
        text: "<@U-BOT> db-02 메모리 확인",
        ts: "1700000200.111",
        thread_ts: "1700000000.123",
      },
    });
    const response = await request(
      createApp({ config, repository, onRequestAccepted: wake }),
    )
      .post("/slack/events")
      .set(signedHeaders(rawBody))
      .send(rawBody);

    expect(response.status).toBe(200);
    expect(response.body.clarifies).toBeUndefined();
    expect(repository.savedRequests[0]?.parentRequestId).toBeNull();
  });

  it("does not look for a parent when the message is not in a thread", async () => {
    const rawBody = JSON.stringify({
      type: "event_callback",
      event_id: "Ev321",
      event_time: 1_700_000_300,
      event: {
        type: "app_mention",
        channel: "C-QUESTIONS",
        user: "U1",
        text: "<@U-BOT> web-01 CPU",
        ts: "1700000300.222",
      },
    });
    await request(createApp({ config, repository, onRequestAccepted: wake }))
      .post("/slack/events")
      .set(signedHeaders(rawBody))
      .send(rawBody);

    expect(repository.findPendingClarification).not.toHaveBeenCalled();
  });

  it("acknowledges duplicates without dispatching twice", async () => {
    repository.saveResult = { created: false, requestId: "REQ-EXISTING" };
    const rawBody = JSON.stringify({
      type: "event_callback",
      event_id: "Ev123",
      event: {
        type: "message",
        channel: "C-QUESTIONS",
        user: "U1",
        text: "web-01 장애",
        ts: "1700000000.123",
      },
    });
    const response = await request(
      createApp({ config, repository, onRequestAccepted: wake }),
    )
      .post("/slack/events")
      .set(signedHeaders(rawBody))
      .send(rawBody);

    expect(response.body.duplicate).toBe(true);
    expect(wake).not.toHaveBeenCalled();
  });

  it("rejects an invalid signature and ignores bot messages", async () => {
    const invalid = await request(createApp({ config, repository }))
      .post("/slack/events")
      .set("content-type", "application/json")
      .send("{}");
    expect(invalid.status).toBe(401);

    const rawBody = JSON.stringify({
      type: "event_callback",
      event_id: "EvBot",
      event: {
        type: "message",
        channel: "C-QUESTIONS",
        user: "U-BOT",
        bot_id: "B1",
        text: "ignore",
        ts: "1",
      },
    });
    const ignored = await request(createApp({ config, repository }))
      .post("/slack/events")
      .set(signedHeaders(rawBody))
      .send(rawBody);
    expect(ignored.status).toBe(200);
    expect(ignored.body.ignored).toBe(true);
    expect(repository.saveSlackRequest).not.toHaveBeenCalled();
  });
});

describe("report feedback", () => {
  let repository: FakeRepository;

  beforeEach(() => {
    repository = new FakeRepository();
  });

  function reactionBody(overrides: Record<string, unknown> = {}): string {
    return JSON.stringify({
      type: "event_callback",
      event_id: "EvReact",
      event_time: 1_700_000_400,
      event: {
        type: "reaction_added",
        user: "U1",
        reaction: "white_check_mark",
        item: { type: "message", channel: "C-ANSWERS", ts: "1700000000.500" },
        ...overrides,
      },
    });
  }

  async function post(rawBody: string, appConfig = config) {
    return request(createApp({ config: appConfig, repository }))
      .post("/slack/events")
      .set(signedHeaders(rawBody))
      .send(rawBody);
  }

  it("records a verdict from a reaction on a published report", async () => {
    const response = await post(reactionBody());

    expect(response.status).toBe(200);
    expect(response.body).toEqual({
      request_id: "REQ-REPORT",
      label: "correct",
      labeled: true,
    });
    expect(repository.findReportByMessage).toHaveBeenCalledWith(
      "C-ANSWERS",
      "1700000000.500",
    );
    expect(repository.saveReportFeedback).toHaveBeenCalledWith({
      requestId: "REQ-REPORT",
      userId: "U1",
      reaction: "white_check_mark",
      label: "correct",
    });
  });

  it("ignores an emoji that carries no verdict without touching the database", async () => {
    const response = await post(reactionBody({ reaction: "eyes" }));

    expect(response.body).toEqual({ ignored: true });
    expect(repository.findReportByMessage).not.toHaveBeenCalled();
  });

  it("strips a skin tone before looking up the verdict", async () => {
    await post(reactionBody({ reaction: "white_check_mark::skin-tone-3" }));

    expect(repository.saveReportFeedback).toHaveBeenCalledWith(
      expect.objectContaining({ reaction: "white_check_mark", label: "correct" }),
    );
  });

  it("ignores a reaction on a message that is not a report", async () => {
    repository.report = null;
    const response = await post(reactionBody({ reaction: "x" }));

    expect(response.body).toEqual({ ignored: true });
    expect(repository.saveReportFeedback).not.toHaveBeenCalled();
  });

  it("undoes the verdict when the reaction is taken back", async () => {
    const response = await post(
      reactionBody({ type: "reaction_removed", reaction: "x" }),
    );

    expect(response.body).toEqual({
      request_id: "REQ-REPORT",
      label: "incorrect",
      removed: true,
    });
    expect(repository.removeReportFeedback).toHaveBeenCalledWith({
      requestId: "REQ-REPORT",
      userId: "U1",
      reaction: "x",
    });
    expect(repository.saveReportFeedback).not.toHaveBeenCalled();
  });

  it("asks for the correction in the report thread after a negative verdict", async () => {
    repository.feedbackResult = { created: true, shouldAskForCorrection: true };
    const fetchMock = vi.fn(
      async (_url: string, _init: RequestInit) =>
        new Response(JSON.stringify({ ok: true })),
    );
    vi.stubGlobal("fetch", fetchMock);

    try {
      await post(reactionBody({ reaction: "x" }), {
        ...config,
        slackBotToken: "xoxb-test",
      });

      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
      const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
      expect(body).toMatchObject({
        channel: "C-ANSWERS",
        // The thread root, not the report message the reaction was left on.
        thread_ts: "1700000000.100",
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("still records the verdict when no bot token is configured", async () => {
    repository.feedbackResult = { created: true, shouldAskForCorrection: true };
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    try {
      const response = await post(reactionBody({ reaction: "x" }));

      expect(response.body.label).toBe("incorrect");
      expect(fetchMock).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("stores a reply under a report as the written correction", async () => {
    const rawBody = JSON.stringify({
      type: "event_callback",
      event_id: "EvNote",
      event_time: 1_700_000_500,
      event: {
        type: "message",
        channel: "C-ANSWERS",
        user: "U1",
        text: "실제 원인은 배치 잡이 디스크를 채운 것이었어",
        ts: "1700000500.777",
        thread_ts: "1700000000.100",
      },
    });
    const response = await post(rawBody);

    expect(response.body).toEqual({
      request_id: "REQ-REPORT",
      note_recorded: true,
    });
    expect(repository.findReportByThread).toHaveBeenCalledWith(
      "C-ANSWERS",
      "1700000000.100",
    );
    expect(repository.saveReportNote).toHaveBeenCalledWith({
      requestId: "REQ-REPORT",
      userId: "U1",
      slackMessageTs: "1700000500.777",
      note: "실제 원인은 배치 잡이 디스크를 채운 것이었어",
    });
    expect(repository.saveSlackRequest).not.toHaveBeenCalled();
  });

  it("does not treat the bot's own thread reply as a correction", async () => {
    const rawBody = JSON.stringify({
      type: "event_callback",
      event_id: "EvBotNote",
      event: {
        type: "message",
        channel: "C-ANSWERS",
        user: "U-BOT",
        bot_id: "B1",
        text: "실제 원인이 무엇이었는지 남겨 주세요",
        ts: "1700000600.111",
        thread_ts: "1700000000.100",
      },
    });
    const response = await post(rawBody);

    expect(response.body).toEqual({ ignored: true });
    expect(repository.saveReportNote).not.toHaveBeenCalled();
  });

  it("leaves the question channel on the clarification path", async () => {
    repository.pendingClarification = { requestId: "REQ-PARENT", question: "CPU?" };
    const rawBody = JSON.stringify({
      type: "event_callback",
      event_id: "EvQuestionThread",
      event_time: 1_700_000_600,
      event: {
        type: "message",
        channel: "C-QUESTIONS",
        user: "U1",
        text: "web-01 이야",
        ts: "1700000600.222",
        thread_ts: "1700000000.123",
      },
    });
    const response = await post(rawBody);

    expect(response.body.clarifies).toBe("REQ-PARENT");
    expect(repository.saveReportNote).not.toHaveBeenCalled();
  });
});

describe("internal API", () => {
  it("requires the internal token and validates agent-run payloads", async () => {
    const repository = new FakeRepository();
    const app = createApp({ config, repository });
    const unauthorized = await request(app)
      .post("/internal/requests/REQ-TEST/agent-runs")
      .send({});
    expect(unauthorized.status).toBe(401);

    const created = await request(app)
      .post("/internal/requests/REQ-TEST/agent-runs")
      .set("x-aiops-internal-token", internalToken)
      .send({
        stage: "question_analyzer",
        status: "succeeded",
        model: "gpt-5.4-mini",
        output: { parse_status: "ready" },
      });
    expect(created.status).toBe(201);
    expect(repository.recordAgentRun).toHaveBeenCalledOnce();
  });
});
