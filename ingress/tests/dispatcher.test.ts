import { describe, expect, it, vi } from "vitest";
import { Dispatcher, lockSecondsFor } from "../src/dispatcher.js";
import type {
  AgentRunInput,
  DispatchJob,
  ReportInput,
  RequestRepository,
  SystemErrorInput,
} from "../src/types.js";

function repositoryWithJob(job: DispatchJob): RequestRepository {
  let claimed = false;
  return {
    ping: vi.fn(async () => undefined),
    listTemplates: vi.fn(async () => []),
    getTemplate: vi.fn(async () => null),
    saveTemplate: vi.fn(async () => ({ version: 1, changed: true, created: true })),
    deleteTemplate: vi.fn(async () => true),
    saveSlackRequest: vi.fn(async () => ({ created: true, requestId: job.requestId })),
    findPendingClarification: vi.fn(async () => null),
    claimDispatch: vi.fn(async () => {
      if (claimed) return null;
      claimed = true;
      return job;
    }),
    completeDispatch: vi.fn(async () => undefined),
    retryDispatch: vi.fn(async () => undefined),
    updateRequestStatus: vi.fn(async () => true),
    recordAgentRun: vi.fn(async (_id: string, _input: AgentRunInput) => true),
    saveReport: vi.fn(async (_id: string, _input: ReportInput) => true),
    recordSystemError: vi.fn(async (_input: SystemErrorInput) => undefined),
    getRequest: vi.fn(async () => null),
    findReportByMessage: vi.fn(async () => null),
    findReportByThread: vi.fn(async () => null),
    saveReportFeedback: vi.fn(async () => ({
      created: true,
      shouldAskForCorrection: false,
    })),
    removeReportFeedback: vi.fn(async () => true),
    saveReportNote: vi.fn(async () => true),
  };
}

const job: DispatchJob = {
  id: 7,
  requestId: "REQ-1",
  attempts: 1,
  payload: {
    request_id: "REQ-1",
    slack_event_id: "EV1",
    team_id: "T1",
    channel_id: "C1",
    user_id: "U1",
    message_ts: "1.2",
    thread_ts: null,
    question: "장애 조사",
    received_at: "2026-01-01T00:00:00.000Z",
    parent_request_id: null,
    prior_question: null,
    parent_ack_ts: null,
  },
};

describe("dispatch lock", () => {
  // The lock used to be a fixed 30 seconds while a delivery could take longer,
  // so a slow one outlived its own claim and a second dispatcher could run the
  // same investigation again. Now that a delivery is the investigation, the
  // margin this leaves is the only thing preventing that.
  it.each([1_000, 10_000, 60_000])(
    "outlives a delivery that takes the full %dms timeout",
    (timeoutMs) => {
      expect(lockSecondsFor(timeoutMs) * 1_000).toBeGreaterThan(timeoutMs);
    },
  );

  it("passes the derived hold to the repository rather than a fixed one", async () => {
    const repository = repositoryWithJob(job);
    const dispatcher = new Dispatcher({
      repository,
      internalToken: "token",
      intervalMs: 1000,
      timeoutMs: 60_000,
      deliver: vi.fn(async () => undefined),
    });

    await dispatcher.runOnce();

    expect(repository.claimDispatch).toHaveBeenCalledWith(lockSecondsFor(60_000));
  });
});

describe("delivering a claimed request", () => {
  it("hands the queued payload to the deliver function and completes", async () => {
    const repository = repositoryWithJob(job);
    const deliver = vi.fn(async () => undefined);
    const dispatcher = new Dispatcher({
      repository,
      internalToken: "token",
      intervalMs: 1000,
      timeoutMs: 5000,
      deliver,
    });

    await dispatcher.runOnce();

    expect(deliver).toHaveBeenCalledWith(job);
    expect(repository.completeDispatch).toHaveBeenCalledWith(7);
    expect(repository.retryDispatch).not.toHaveBeenCalled();
  });

  it("schedules a retry carrying the reason it failed", async () => {
    const repository = repositoryWithJob({ ...job, attempts: 3 });
    const dispatcher = new Dispatcher({
      repository,
      internalToken: "token",
      intervalMs: 1000,
      timeoutMs: 5000,
      deliver: vi.fn(async () => {
        throw new Error("RCA service returned HTTP 503");
      }),
    });

    await dispatcher.runOnce();

    expect(repository.retryDispatch).toHaveBeenCalledWith(
      7,
      8,
      "RCA service returned HTTP 503",
    );
  });
});

describe("giving up on a delivery", () => {
  /**
   * The retry was unbounded. That sounds harmless -- the backoff tops out at
   * five minutes -- but the harm lands on the asker: a question whose delivery
   * can never succeed sat in the queue forever with an acknowledgement already
   * posted, no answer coming, and nothing anywhere saying so.
   */
  function failingRepository(attempts: number) {
    const calls = {
      retryDispatch: [] as unknown[],
      completeDispatch: [] as unknown[],
      updateRequestStatus: [] as unknown[],
      recordSystemError: [] as unknown[],
    };
    const job = {
      id: 1,
      requestId: "REQ-1",
      attempts,
      payload: { request_id: "REQ-1" },
    };
    let claimed = false;
    const repository = {
      async claimDispatch() {
        if (claimed) return null;
        claimed = true;
        return job;
      },
      async completeDispatch(id: number) {
        calls.completeDispatch.push(id);
      },
      async retryDispatch(id: number, delay: number, error: string) {
        calls.retryDispatch.push({ id, delay, error });
      },
      async updateRequestStatus(requestId: string, status: string, error?: string) {
        calls.updateRequestStatus.push({ requestId, status, error });
        return true;
      },
      async recordSystemError(input: unknown) {
        calls.recordSystemError.push(input);
      },
    } as unknown as RequestRepository;
    return { repository, calls };
  }

  async function runOnce(attempts: number) {
    const { repository, calls } = failingRepository(attempts);
    const dispatcher = new Dispatcher({
      repository,
      internalToken: "t",
      intervalMs: 1_000,
      timeoutMs: 5_000,
      deliver: async () => {
        throw new Error("RCA service returned HTTP 500");
      },
    });
    await dispatcher.runOnce();
    return calls;
  }

  it("keeps retrying while attempts remain", async () => {
    const calls = await runOnce(0);
    expect(calls.retryDispatch).toHaveLength(1);
    expect(calls.updateRequestStatus).toHaveLength(0);
  });

  it("stops after the last attempt", async () => {
    const calls = await runOnce(11);
    expect(calls.retryDispatch).toHaveLength(0);
    expect(calls.completeDispatch).toEqual([1]);
  });

  it("marks the request failed so it stops claiming to be in progress", async () => {
    const calls = await runOnce(11);
    expect(calls.updateRequestStatus).toHaveLength(1);
    const update = calls.updateRequestStatus[0] as { status: string; error: string };
    expect(update.status).toBe("failed");
    expect(update.error).toContain("the investigation failed 12 times");
  });

  it("records the error where the error channel reads from", async () => {
    // Without this the asker never learns their question died.
    const calls = await runOnce(11);
    expect(calls.recordSystemError).toHaveLength(1);
  });
});
