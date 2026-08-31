import { describe, expect, it, vi } from "vitest";
import {
  Dispatcher,
  lockSecondsFor,
  MAX_DISPATCH_ATTEMPTS,
  type Announce,
} from "../src/dispatcher.js";
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
    slack_ack_ts: null,
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
      announce: vi.fn(async () => undefined),
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
      announce: vi.fn(async () => undefined),
    });

    await dispatcher.runOnce();

    expect(deliver).toHaveBeenCalledWith(job);
    expect(repository.completeDispatch).toHaveBeenCalledWith(7);
    expect(repository.retryDispatch).not.toHaveBeenCalled();
  });

  it("does not re-run an investigation whose only failure was closing the row", async () => {
    // deliver posts the report before it returns, so a completeDispatch that
    // throws afterwards is bookkeeping. Sharing a try with the delivery made
    // it indistinguishable from a failed investigation: the job went back on
    // the queue and the whole thing ran again. At the ceiling it was worse --
    // abandon told the asker a question that had just been answered failed.
    const repository = repositoryWithJob({ ...job, attempts: MAX_DISPATCH_ATTEMPTS });
    repository.completeDispatch = vi.fn(async () => {
      throw new Error("Connection terminated unexpectedly");
    });
    const announce = vi.fn(async () => undefined);
    const dispatcher = new Dispatcher({
      repository,
      internalToken: "token",
      intervalMs: 1000,
      timeoutMs: 5000,
      deliver: vi.fn(async () => undefined),
      announce,
    });

    await dispatcher.runOnce();

    expect(repository.retryDispatch).not.toHaveBeenCalled();
    expect(repository.updateRequestStatus).not.toHaveBeenCalled();
    expect(announce).not.toHaveBeenCalled();
    // Recorded rather than thrown: the claim loop has to survive it.
    expect(repository.recordSystemError).toHaveBeenCalledTimes(1);
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
      announce: vi.fn(async () => undefined),
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

  /**
   * The claim that has no attempt left after it, and the one before it.
   *
   * Both taken off the constant rather than written out, so moving the ceiling
   * moves them with it instead of leaving these tests pointed somewhere past
   * the edge they exist to pin. The edge is pinned from both sides on purpose:
   * asserting only that the last claim gives up cannot tell an off-by-one from
   * a correct count, and this file was passing while there was one.
   */
  const LAST_ATTEMPT = MAX_DISPATCH_ATTEMPTS;
  const SECOND_TO_LAST_ATTEMPT = MAX_DISPATCH_ATTEMPTS - 1;
  const FIRST_ATTEMPT = 1;

  async function runOnce(attempts: number, announce?: Announce) {
    const { repository, calls } = failingRepository(attempts);
    const announced: { requestId: string; reason: string }[] = [];
    const dispatcher = new Dispatcher({
      repository,
      internalToken: "t",
      intervalMs: 1_000,
      timeoutMs: 5_000,
      deliver: async () => {
        throw new Error("RCA service returned HTTP 500");
      },
      announce:
        announce ??
        (async (job, reason) => {
          announced.push({ requestId: job.requestId, reason });
        }),
    });
    await dispatcher.runOnce();
    return { ...calls, announced };
  }

  it("keeps retrying while attempts remain", async () => {
    // The first claim. claimDispatch increments before it returns, so 1 is the
    // lowest a real job carries -- 0 pinned a state the queue cannot produce.
    const calls = await runOnce(FIRST_ATTEMPT);
    expect(calls.retryDispatch).toHaveLength(1);
    expect(calls.updateRequestStatus).toHaveLength(0);
  });

  it("stops after the last attempt", async () => {
    const calls = await runOnce(LAST_ATTEMPT);
    expect(calls.retryDispatch).toHaveLength(0);
    expect(calls.completeDispatch).toEqual([1]);
  });

  it("still has one left on the claim before it", async () => {
    // The other half of the edge. claimDispatch increments before it returns,
    // so this is the claim numbered one below the ceiling, and it is a retry.
    const calls = await runOnce(SECOND_TO_LAST_ATTEMPT);

    expect(calls.retryDispatch).toHaveLength(1);
    expect(calls.updateRequestStatus).toHaveLength(0);
    expect(calls.announced).toEqual([]);
  });

  it("marks the request failed so it stops claiming to be in progress", async () => {
    const calls = await runOnce(LAST_ATTEMPT);
    expect(calls.updateRequestStatus).toHaveLength(1);
    const update = calls.updateRequestStatus[0] as { status: string; error: string };
    expect(update.status).toBe("failed");
    // The count it reports is the number of deliveries actually made.
    expect(update.error).toContain(
      `the investigation failed ${MAX_DISPATCH_ATTEMPTS} times`,
    );
  });

  it("writes the failure down for whoever operates this", async () => {
    // Not how the asker finds out. This used to be the whole of it, back when
    // n8n's error workflow read the table and posted what it found; nothing
    // reads it now. The announcement below is the notification.
    const calls = await runOnce(LAST_ATTEMPT);
    expect(calls.recordSystemError).toHaveLength(1);
  });

  it("tells the asker their question died", async () => {
    const calls = await runOnce(LAST_ATTEMPT);

    expect(calls.announced).toEqual([
      { requestId: "REQ-1", reason: "RCA service returned HTTP 500" },
    ]);
  });

  it("says nothing while attempts remain", async () => {
    // A retry is not news. The asker hears once, when there is nothing left.
    const calls = await runOnce(FIRST_ATTEMPT);

    expect(calls.announced).toEqual([]);
  });

  it("still tells the asker when an earlier closing write fails", async () => {
    // The four writes are independent and the announcement is last, so a
    // status update that threw used to take the whole sequence with it --
    // leaving the request reading as in progress and the asker hearing
    // nothing, which is the failure the announcement was added to fix.
    const announced: string[] = [];
    const errors: unknown[] = [];
    const repository = {
      claimDispatch: (() => {
        let claimed = false;
        return async () => {
          if (claimed) return null;
          claimed = true;
          return { id: 1, requestId: "REQ-1", attempts: LAST_ATTEMPT, payload: {} };
        };
      })(),
      async completeDispatch() {},
      async retryDispatch() {},
      async updateRequestStatus() {
        throw new Error("Connection terminated unexpectedly");
      },
      async recordSystemError(input: unknown) {
        errors.push(input);
      },
    } as unknown as RequestRepository;

    const dispatcher = new Dispatcher({
      repository,
      internalToken: "t",
      intervalMs: 1_000,
      timeoutMs: 5_000,
      deliver: async () => {
        throw new Error("RCA service returned HTTP 500");
      },
      announce: async (_job, reason) => {
        announced.push(reason);
      },
    });

    await dispatcher.runOnce();

    expect(announced).toEqual(["RCA service returned HTTP 500"]);
    expect(JSON.stringify(errors)).toContain("could not be marked failed");
  });

  it("survives an announcement that cannot be delivered", async () => {
    // This runs inside the catch around a delivery, where a throw leaves the
    // claim loop -- so Slack being down would stop the dispatcher outright.
    // The job stays closed, and the failure to announce is recorded too.
    const calls = await runOnce(LAST_ATTEMPT, async () => {
      throw new Error("slack chat.postMessage failed: channel_not_found");
    });

    expect(calls.completeDispatch).toEqual([1]);
    expect(calls.recordSystemError).toHaveLength(2);
    expect(JSON.stringify(calls.recordSystemError[1])).toContain("channel_not_found");
  });
});
