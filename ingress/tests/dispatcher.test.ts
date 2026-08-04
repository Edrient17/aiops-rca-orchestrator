import { describe, expect, it, vi } from "vitest";
import { N8nDispatcher } from "../src/dispatcher.js";
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

describe("n8n dispatcher", () => {
  it("marks successful webhook delivery complete", async () => {
    const repository = repositoryWithJob(job);
    const fetchImpl = vi.fn(async () => new Response(null, { status: 202 }));
    const dispatcher = new N8nDispatcher({
      repository,
      webhookUrl: "http://n8n/webhook/aiops-process",
      internalToken: "token",
      intervalMs: 1000,
      timeoutMs: 5000,
      fetchImpl,
    });

    await dispatcher.runOnce();

    expect(fetchImpl).toHaveBeenCalledWith(
      "http://n8n/webhook/aiops-process",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(job.payload),
      }),
    );
    expect(repository.completeDispatch).toHaveBeenCalledWith(7);
    expect(repository.retryDispatch).not.toHaveBeenCalled();
  });

  it("schedules a retry when n8n is unavailable", async () => {
    const repository = repositoryWithJob({ ...job, attempts: 3 });
    const dispatcher = new N8nDispatcher({
      repository,
      webhookUrl: "http://n8n/webhook/aiops-process",
      internalToken: "token",
      intervalMs: 1000,
      timeoutMs: 5000,
      fetchImpl: vi.fn(async () => new Response(null, { status: 503 })),
    });

    await dispatcher.runOnce();

    expect(repository.retryDispatch).toHaveBeenCalledWith(
      7,
      8,
      "n8n webhook returned HTTP 503",
    );
  });
});
