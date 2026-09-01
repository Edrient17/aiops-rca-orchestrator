/**
 * The ceiling that actually applies to an investigation call, and seeing it.
 *
 * One Slack question was investigated three times: ingress gave up at 300
 * seconds, twice, while rca-api went on to finish the run it had stopped
 * listening to. RCA_TIMEOUT_MS said 900 seconds and the code passed it to
 * `AbortSignal.timeout`, which is not the ceiling that bit -- undici stops
 * waiting when response headers have not arrived within `headersTimeout`, and
 * that defaults to 300 seconds. rca-api sends no headers until the whole
 * investigation is written, so five minutes was the real limit on every
 * investigation this service has ever made.
 *
 * Nothing showed it. The dispatcher logged nothing on a retry, and the one
 * record it wrote -- last_error on the queue row -- is cleared by
 * completeDispatch as soon as a later attempt succeeds. The same question had
 * failed the same way eleven days earlier.
 */

import http from "node:http";
import type { AddressInfo } from "node:net";
import { Agent, fetch as undiciFetch } from "undici";
import { describe, expect, it, vi } from "vitest";
import { Dispatcher, MAX_DISPATCH_ATTEMPTS } from "../src/dispatcher.js";
import { envSchema } from "../src/config.js";
import type { DispatchJob } from "../src/types.js";

/** A server that answers only after `delayMs`, like a running investigation. */
async function slowServer(delayMs: number) {
  const server = http.createServer((_request, response) => {
    setTimeout(() => response.end(JSON.stringify({ ok: true })), delayMs);
  });
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  const { port } = server.address() as AddressInfo;
  return {
    url: `http://127.0.0.1:${port}/`,
    close: () => new Promise<void>((done) => server.close(() => done())),
  };
}

describe("what stops an investigation call", () => {
  /**
   * The mechanism, at a scale a test can wait for. Real numbers are 300s
   * against 900s; these are 0.5s against 20s, and the shape is the same.
   */
  it("is the dispatcher's ceiling, not the abort signal", async () => {
    const server = await slowServer(3000);
    const impatient = new Agent({ headersTimeout: 500, bodyTimeout: 500 });
    let cause: string | undefined;
    try {
      // A signal forty times more generous than the answer needs. It does not
      // save the call, which is exactly the bug.
      await undiciFetch(server.url, {
        dispatcher: impatient,
        signal: AbortSignal.timeout(20_000),
      });
    } catch (error) {
      // Read off the cause rather than matched on the error: `cause` is not
      // enumerable, so a structural match against the thrown object misses it.
      cause = (error as { cause?: { code?: string } }).cause?.code;
    } finally {
      await impatient.close();
      await server.close();
    }
    expect(cause).toBe("UND_ERR_HEADERS_TIMEOUT");
  });

  it("lets the same slow answer through once the ceiling is raised", async () => {
    const server = await slowServer(1500);
    const patient = new Agent({ headersTimeout: 20_000, bodyTimeout: 20_000 });
    try {
      const response = await undiciFetch(server.url, { dispatcher: patient });
      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({ ok: true });
    } finally {
      await patient.close();
      await server.close();
    }
  });

  /**
   * The half that makes the above reach production: the investigation call has
   * to carry a dispatcher at all. Without one it runs under undici's default
   * and RCA_TIMEOUT_MS is decoration.
   */
  it("is carried on the investigation call itself", async () => {
    const { runInvestigation } = await import("../src/pipeline.js");
    let options: Record<string, unknown> | undefined;

    const fetchImpl = vi.fn(async (url: unknown, init: unknown) => {
      // The same injected fetch serves the Slack post that follows, so the two
      // are answered apart; only the investigation call is under inspection.
      if (String(url).includes("slack.com")) {
        return new Response(JSON.stringify({ ok: true, ts: "1.2" }), { status: 200 });
      }
      options = init as Record<string, unknown>;
      return new Response(
        JSON.stringify({
          status: "unsupported",
          parsed_request: { parse_status: "unsupported", ambiguities: [] },
          agent_runs: [{ stage: "question_analyzer", status: "succeeded" }],
        }),
        { status: 200 },
      );
    });

    const repository = {
      findRequestStatus: async () => null,
      findReportByRequest: async () => null,
      updateRequestStatus: async () => true,
      listTemplates: async () => [],
      recordAgentRun: async () => true,
    } as never;

    await runInvestigation(
      {
        request_id: "REQ-1",
        channel_id: "C1",
        user_id: "U1",
        message_ts: "1.1",
        question: "왜 멈췄어?",
        received_at: "2026-09-01T06:40:37.000Z",
        slack_ack_ts: "1.0",
      },
      { repository, fetchImpl: fetchImpl as never },
      {
        answerChannelId: "C-ANSWERS",
        rcaApiUrl: "http://rca-api:8090",
        internalToken: "t",
        botToken: "b",
        rcaTimeoutMs: 900_000,
        slackTimeoutMs: 30_000,
        format: {},
      },
    );

    expect(options?.dispatcher).toBeInstanceOf(Agent);
  });
});

describe("a delivery that failed and will be retried", () => {
  function job(attempts: number): DispatchJob {
    return {
      id: 1,
      requestId: "REQ-1",
      attempts,
      payload: { request_id: "REQ-1" } as never,
    };
  }

  function dispatcherThatFails(attempts: number, retried: unknown[]) {
    let handed = false;
    return new Dispatcher({
      repository: {
        claimDispatch: async () => (handed ? null : ((handed = true), job(attempts))),
        retryDispatch: async (...args: unknown[]) => void retried.push(args),
        completeDispatch: async () => {},
        recordSystemError: async () => {},
        updateRequestStatus: async () => true,
      } as never,
      internalToken: "t",
      intervalMs: 1000,
      timeoutMs: 900_000,
      deliver: async () => {
        throw new Error("fetch failed: UND_ERR_HEADERS_TIMEOUT");
      },
      announce: async () => {},
    });
  }

  it("says so, rather than leaving the retry invisible", async () => {
    const logged: string[] = [];
    const spy = vi.spyOn(console, "error").mockImplementation((line) => {
      logged.push(String(line));
    });
    try {
      await dispatcherThatFails(1, []).runOnce();
    } finally {
      spy.mockRestore();
    }

    const entry = logged.map((line) => JSON.parse(line)).find(
      (item) => item.message === "delivery_failed",
    );
    expect(entry).toMatchObject({
      level: "warn",
      request_id: "REQ-1",
      attempt: 1,
      of: MAX_DISPATCH_ATTEMPTS,
      retry_in_seconds: 2,
    });
    // The cause has to survive: completeDispatch clears the queue's copy the
    // moment a later attempt succeeds, so this line is the only record left of
    // why the earlier ones did not.
    expect(entry.detail).toContain("UND_ERR_HEADERS_TIMEOUT");
  });

  it("still schedules the retry it announced", async () => {
    const retried: unknown[] = [];
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      await dispatcherThatFails(3, retried).runOnce();
    } finally {
      spy.mockRestore();
    }
    expect(retried[0]).toEqual([1, 8, "fetch failed: UND_ERR_HEADERS_TIMEOUT"]);
  });
});

describe("the three ceilings, in order", () => {
  const BASE = {
    DATABASE_URL: "postgres://x",
    SLACK_SIGNING_SECRET: "s",
    SLACK_QUESTION_CHANNEL_ID: "C1",
    AIOPS_INTERNAL_TOKEN: "x".repeat(24),
    SLACK_BOT_TOKEN: "b",
    RCA_API_URL: "http://rca-api:8090",
    SLACK_ANSWER_CHANNEL_ID: "C2",
  };

  it("defaults above rca-api's own collection ceiling", () => {
    // 600s of collection plus what writing costs. The default has to clear it,
    // or the outermost limit is not the largest one.
    expect(envSchema.parse(BASE).RCA_TIMEOUT_MS).toBeGreaterThan(660_000);
  });

  it("refuses a timeout that would cut collection short", () => {
    // The inversion this is here to prevent: five minutes was the real ceiling
    // for months, under a setting that said fifteen.
    expect(() =>
      envSchema.parse({ ...BASE, RCA_TIMEOUT_MS: "300000" }),
    ).toThrow();
  });

  it("still accepts a deliberately longer one", () => {
    expect(
      envSchema.parse({ ...BASE, RCA_TIMEOUT_MS: "1200000" }).RCA_TIMEOUT_MS,
    ).toBe(1_200_000);
  });
});
