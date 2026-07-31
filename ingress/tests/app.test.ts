import request from "supertest";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createApp } from "../src/app.js";
import { createSlackSignature } from "../src/slack.js";
import type {
  AgentRunInput,
  DispatchJob,
  ReportInput,
  RequestRepository,
  SaveRequestResult,
  SystemErrorInput,
} from "../src/types.js";

class FakeRepository implements RequestRepository {
  saveResult: SaveRequestResult = { created: true, requestId: "REQ-TEST" };
  saveSlackRequest = vi.fn(async () => this.saveResult);
  ping = vi.fn(async () => undefined);
  claimDispatch = vi.fn(async (): Promise<DispatchJob | null> => null);
  completeDispatch = vi.fn(async () => undefined);
  retryDispatch = vi.fn(async () => undefined);
  updateRequestStatus = vi.fn(async () => true);
  recordAgentRun = vi.fn(async (_id: string, _input: AgentRunInput) => true);
  saveReport = vi.fn(async (_id: string, _input: ReportInput) => true);
  recordSystemError = vi.fn(async (_input: SystemErrorInput) => undefined);
  getRequest = vi.fn(async () => ({ request_id: "REQ-TEST" }));
}

const signingSecret = "test-signing-secret";
const internalToken = "internal-token-with-at-least-24-characters";
const config = {
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
