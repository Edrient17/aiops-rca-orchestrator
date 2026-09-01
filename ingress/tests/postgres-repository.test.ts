/**
 * The SQL, against a real Postgres.
 *
 * postgres-repository.ts is the largest file in this repository and had no
 * test of any kind. Every other suite replaces `RequestRepository` with an
 * object literal, so the queries themselves -- the claim under FOR UPDATE SKIP
 * LOCKED, the untargeted upsert that has to catch two different keys, the
 * guard that stops a late failure retracting a published report, the version
 * that continues from history rather than from the live row -- were carried
 * entirely by the comments explaining them.
 *
 * Those comments make claims that are decidable, and this decides them. It
 * needs a database, which is why it is the one suite that skips itself: CI
 * runs a Postgres service and sets TEST_DATABASE_URL, and a developer without
 * one still gets a green run rather than a wall of connection errors.
 *
 *   docker run --rm -e POSTGRES_PASSWORD=test -p 5433:5432 postgres:17-alpine
 *   TEST_DATABASE_URL=postgres://postgres:test@127.0.0.1:5433/postgres npm test
 *
 * The schema is applied from database/migrations, so this also proves those
 * files still apply to an empty database in filename order -- something
 * db-migrate does on every deploy and nothing checked.
 */

import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Pool } from "pg";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { PostgresRequestRepository } from "../src/postgres-repository.js";
import { lockSecondsFor } from "../src/dispatcher.js";
import type { AcceptedSlackRequest, ReportInput } from "../src/types.js";

const DATABASE_URL = process.env.TEST_DATABASE_URL;
const HERE = resolve(fileURLToPath(new URL("./", import.meta.url)));
const MIGRATIONS = resolve(HERE, "..", "..", "database", "migrations");

const TABLES = [
  "aiops_report_notes",
  "aiops_report_feedback",
  "aiops_tool_calls",
  "aiops_agent_runs",
  "aiops_reports",
  "aiops_dispatch_queue",
  "aiops_requests",
  "aiops_system_errors",
  "aiops_report_templates",
  "aiops_report_template_versions",
];

/** A template that satisfies the columns; the shape is checked in zod, not here. */
const TEMPLATE = {
  title: "사고 RCA",
  description: "장애 원인 분석",
  enabled: true,
  collection: { host_selector: { mode: "from_question" as const } },
  output: { sections: [{ id: "summary", heading: "요약" }] },
} as never;

function request(overrides: Partial<AcceptedSlackRequest> = {}): AcceptedSlackRequest {
  return {
    requestId: "REQ-1",
    slackEventId: "Ev1",
    teamId: "T1",
    channelId: "C-QUESTIONS",
    userId: "U1",
    messageTs: "1785900000.000100",
    threadTs: null,
    question: "payment-service가 왜 멈췄어?",
    receivedAt: "2026-08-31T02:31:00.000Z",
    rawPayload: { event_id: "Ev1" },
    ...overrides,
  };
}

const REPORT: ReportInput = {
  parsedRequest: { request_id: "REQ-1" },
  evidencePackage: {
    investigation: {
      tool_calls: [
        {
          tool_call_id: "call-1",
          tool_name: "get_incident_events",
          purpose: "이벤트 확인",
          status: "success",
        },
      ],
    },
  },
  rcaReport: { title: "보고서" },
  slackMarkdown: "📋 *보고서*",
  slackChannelId: "C-ANSWERS",
  slackMessageTs: "1785900100.000200",
};

describe.skipIf(!DATABASE_URL)("the repository against a real Postgres", () => {
  let pool: Pool;
  let repository: PostgresRequestRepository;

  beforeAll(async () => {
    pool = new Pool({ connectionString: DATABASE_URL, max: 6 });
    // Applied the way db-migrate applies them: every file, in filename order,
    // on every start. They have to be idempotent, so running them here twice
    // across two Node versions is the same bargain the deploy makes.
    for (const name of readdirSync(MIGRATIONS).filter((f) => f.endsWith(".sql")).sort()) {
      await pool.query(readFileSync(resolve(MIGRATIONS, name), "utf8"));
    }
    repository = new PostgresRequestRepository(pool);
  }, 60_000);

  afterAll(async () => {
    await pool?.end();
  });

  beforeEach(async () => {
    await pool.query(`TRUNCATE ${TABLES.join(", ")} RESTART IDENTITY CASCADE`);
  });

  describe("accepting a Slack message", () => {
    it("stores the request and queues it", async () => {
      const saved = await repository.saveSlackRequest(request());
      expect(saved).toEqual({ created: true, requestId: "REQ-1" });

      const queued = await pool.query(
        "SELECT request_id, attempts, dispatched_at FROM aiops_dispatch_queue",
      );
      expect(queued.rows).toEqual([
        { request_id: "REQ-1", attempts: 0, dispatched_at: null },
      ]);
    });

    it("recognises a redelivery of the same event", async () => {
      await repository.saveSlackRequest(request());
      const again = await repository.saveSlackRequest(request());
      expect(again).toEqual({ created: false, requestId: "REQ-1" });
      expect((await pool.query("SELECT 1 FROM aiops_requests")).rowCount).toBe(1);
    });

    /**
     * The reason ON CONFLICT is untargeted. An app subscribed to app_mention
     * and message.channels receives one mention as two events with different
     * ids and the same message, which only the (channel, message_ts) key can
     * catch -- and it answers the question twice if it does not.
     */
    it("recognises one message arriving as two different events", async () => {
      await repository.saveSlackRequest(request());
      const twin = await repository.saveSlackRequest(
        request({ requestId: "REQ-2", slackEventId: "Ev2" }),
      );
      expect(twin.created).toBe(false);
      expect(twin.requestId).toBe("REQ-1");
      expect((await pool.query("SELECT 1 FROM aiops_requests")).rowCount).toBe(1);
    });

    it("marks the parent clarified when a continuation arrives", async () => {
      await repository.saveSlackRequest(request());
      await repository.updateRequestStatus("REQ-1", "needs_clarification");
      await repository.saveSlackRequest(
        request({
          requestId: "REQ-2",
          slackEventId: "Ev2",
          messageTs: "1785900500.000100",
          parentRequestId: "REQ-1",
        }),
      );
      expect(await repository.findRequestStatus("REQ-1")).toBe("clarified");
    });

    it("finds the clarification a threaded reply answers", async () => {
      await repository.saveSlackRequest(request());
      await repository.updateRequestStatus("REQ-1", "needs_clarification");
      const pending = await repository.findPendingClarification(
        "C-QUESTIONS",
        "1785900000.000100",
      );
      expect(pending).toEqual({ requestId: "REQ-1", question: request().question });
    });

    it("offers no clarification once the request has moved on", async () => {
      await repository.saveSlackRequest(request());
      await repository.updateRequestStatus("REQ-1", "completed");
      expect(
        await repository.findPendingClarification("C-QUESTIONS", "1785900000.000100"),
      ).toBeNull();
    });
  });

  describe("claiming a job", () => {
    const lock = lockSecondsFor(900_000);

    it("returns the request joined onto the queue row", async () => {
      await repository.saveSlackRequest(request());
      const job = await repository.claimDispatch(lock);
      expect(job?.requestId).toBe("REQ-1");
      expect(job?.attempts).toBe(1);
      expect(job?.payload.question).toBe(request().question);
      expect(job?.payload.received_at).toBe("2026-08-31T02:31:00.000Z");
    });

    /**
     * The lock has to outlast the delivery, or a second dispatcher claims a
     * job still in flight and investigates the same request twice.
     */
    it("does not hand the same job to a second claim", async () => {
      await repository.saveSlackRequest(request());
      expect(await repository.claimDispatch(lock)).not.toBeNull();
      expect(await repository.claimDispatch(lock)).toBeNull();
    });

    it("counts each claim, so the retry ceiling can be reached", async () => {
      await repository.saveSlackRequest(request());
      const first = await repository.claimDispatch(lock);
      await repository.retryDispatch(first!.id, 0, "boom");
      const second = await repository.claimDispatch(lock);
      expect([first?.attempts, second?.attempts]).toEqual([1, 2]);
    });

    it("leaves a completed job alone for good", async () => {
      await repository.saveSlackRequest(request());
      const job = await repository.claimDispatch(lock);
      await repository.completeDispatch(job!.id);
      expect(await repository.claimDispatch(lock)).toBeNull();
    });

    it("waits out the backoff a retry asked for", async () => {
      await repository.saveSlackRequest(request());
      const job = await repository.claimDispatch(lock);
      await repository.retryDispatch(job!.id, 300, "boom");
      expect(await repository.claimDispatch(lock)).toBeNull();

      const stored = await pool.query("SELECT last_error FROM aiops_dispatch_queue");
      expect(stored.rows[0].last_error).toBe("boom");
    });

    /** A continuation is delivered into the thread its parent opened. */
    it("carries the parent's question and anchor", async () => {
      await repository.saveSlackRequest(request());
      await repository.updateRequestStatus(
        "REQ-1",
        "needs_clarification",
        undefined,
        "1785900050.000001",
      );
      await repository.saveSlackRequest(
        request({
          requestId: "REQ-2",
          slackEventId: "Ev2",
          messageTs: "1785900500.000100",
          parentRequestId: "REQ-1",
          question: "vm-java-docker-2 입니다",
        }),
      );
      await repository.claimDispatch(lock); // REQ-1, claimed first
      const job = await repository.claimDispatch(lock);
      expect(job?.requestId).toBe("REQ-2");
      expect(job?.payload.prior_question).toBe(request().question);
      expect(job?.payload.parent_ack_ts).toBe("1785900050.000001");
    });
  });

  describe("moving a request through its statuses", () => {
    it("records the anchor and keeps it through later changes", async () => {
      await repository.saveSlackRequest(request());
      await repository.updateRequestStatus(
        "REQ-1",
        "analyzing_question",
        undefined,
        "1785900050.000001",
      );
      await repository.updateRequestStatus("REQ-1", "completed");

      const row = await pool.query(
        "SELECT status, slack_ack_ts FROM aiops_requests WHERE request_id = $1",
        ["REQ-1"],
      );
      expect(row.rows[0]).toEqual({
        status: "completed",
        slack_ack_ts: "1785900050.000001",
      });
    });

    /**
     * The dispatcher marks a request failed when it gives up. Doing that after
     * a delivery that already published a report or asked a clarification
     * retracts something the asker has read.
     */
    it.each(["completed", "needs_clarification", "unsupported", "clarified"])(
      "refuses to relabel a %s request as failed",
      async (settled) => {
        await repository.saveSlackRequest(request());
        await repository.updateRequestStatus("REQ-1", settled);
        expect(await repository.updateRequestStatus("REQ-1", "failed", "gave up")).toBe(
          false,
        );
        expect(await repository.findRequestStatus("REQ-1")).toBe(settled);
      },
    );

    it("does mark an unfinished request failed", async () => {
      await repository.saveSlackRequest(request());
      await repository.updateRequestStatus("REQ-1", "analyzing_question");
      expect(await repository.updateRequestStatus("REQ-1", "failed", "gave up")).toBe(
        true,
      );
      expect(await repository.findRequestStatus("REQ-1")).toBe("failed");
    });

    it("applies the same guard when the error is recorded", async () => {
      await repository.saveSlackRequest(request());
      await repository.updateRequestStatus("REQ-1", "needs_clarification");
      await repository.recordSystemError({ requestId: "REQ-1", message: "late failure" });
      expect(await repository.findRequestStatus("REQ-1")).toBe("needs_clarification");
      expect((await pool.query("SELECT 1 FROM aiops_system_errors")).rowCount).toBe(1);
    });

    it("says nothing was written for a request that does not exist", async () => {
      expect(await repository.updateRequestStatus("REQ-NOPE", "completed")).toBe(false);
      expect(await repository.findRequestStatus("REQ-NOPE")).toBeNull();
    });
  });

  describe("publishing a report", () => {
    it("stores it, completes the request and records the tool calls", async () => {
      await repository.saveSlackRequest(request());
      expect(await repository.saveReport("REQ-1", REPORT)).toBe(true);

      expect(await repository.findRequestStatus("REQ-1")).toBe("completed");
      const calls = await pool.query(
        "SELECT tool_call_id, status FROM aiops_tool_calls",
      );
      expect(calls.rows).toEqual([{ tool_call_id: "call-1", status: "success" }]);
    });

    it("writes nothing for a request that does not exist", async () => {
      expect(await repository.saveReport("REQ-NOPE", REPORT)).toBe(false);
      expect((await pool.query("SELECT 1 FROM aiops_reports")).rowCount).toBe(0);
    });

    /** What runInvestigation reads to know an earlier delivery already answered. */
    it("is findable by the request that produced it", async () => {
      await repository.saveSlackRequest(request());
      await repository.saveReport("REQ-1", REPORT);
      expect(await repository.findReportByRequest("REQ-1")).toEqual({
        channelId: "C-ANSWERS",
        messageTs: "1785900100.000200",
      });
    });

    it("is findable by the message it was posted as", async () => {
      await repository.saveSlackRequest(request());
      await repository.updateRequestStatus(
        "REQ-1",
        "analyzing_question",
        undefined,
        "1785900050.000001",
      );
      await repository.saveReport("REQ-1", REPORT);
      expect(
        await repository.findReportByMessage("C-ANSWERS", "1785900100.000200"),
      ).toEqual({ requestId: "REQ-1", threadTs: "1785900050.000001" });
    });

    /**
     * A reply to the report carries the acknowledgement's ts, because the
     * report is itself a reply under it and Slack threads are one level deep.
     */
    it("is findable from the thread a correction is written in", async () => {
      await repository.saveSlackRequest(request());
      await repository.updateRequestStatus(
        "REQ-1",
        "analyzing_question",
        undefined,
        "1785900050.000001",
      );
      await repository.saveReport("REQ-1", REPORT);
      expect(
        await repository.findReportByThread("C-ANSWERS", "1785900050.000001"),
      ).toEqual({ requestId: "REQ-1", threadTs: "1785900050.000001" });
    });
  });

  describe("feedback on a published report", () => {
    beforeEach(async () => {
      await repository.saveSlackRequest(request());
      await repository.saveReport("REQ-1", REPORT);
    });

    it("asks for the truth on the first negative verdict", async () => {
      const saved = await repository.saveReportFeedback({
        requestId: "REQ-1",
        userId: "U1",
        reaction: "x",
        label: "incorrect",
      });
      expect(saved).toEqual({ created: true, shouldAskForCorrection: true });
    });

    it("does not ask again on the second", async () => {
      const first = { requestId: "REQ-1", userId: "U1", reaction: "x", label: "incorrect" } as const;
      await repository.saveReportFeedback(first);
      const second = await repository.saveReportFeedback({
        ...first,
        userId: "U2",
        reaction: "thinking_face",
        label: "partial",
      });
      expect(second.shouldAskForCorrection).toBe(false);
    });

    it("never asks about a report somebody called correct", async () => {
      const saved = await repository.saveReportFeedback({
        requestId: "REQ-1",
        userId: "U1",
        reaction: "white_check_mark",
        label: "correct",
      });
      expect(saved.shouldAskForCorrection).toBe(false);
    });

    it("absorbs the same reaction arriving twice", async () => {
      const input = { requestId: "REQ-1", userId: "U1", reaction: "x", label: "incorrect" } as const;
      await repository.saveReportFeedback(input);
      expect((await repository.saveReportFeedback(input)).created).toBe(false);
    });

    it("removes exactly what adding it created", async () => {
      const input = { requestId: "REQ-1", userId: "U1", reaction: "x" };
      await repository.saveReportFeedback({ ...input, label: "incorrect" });
      expect(await repository.removeReportFeedback(input)).toBe(true);
      expect((await pool.query("SELECT 1 FROM aiops_report_feedback")).rowCount).toBe(0);
    });

    it("keeps a written correction once, however often Slack redelivers", async () => {
      const note = {
        requestId: "REQ-1",
        userId: "U1",
        slackMessageTs: "1785900200.000300",
        note: "실제로는 디스크가 찼습니다",
      };
      expect(await repository.saveReportNote(note)).toBe(true);
      expect(await repository.saveReportNote(note)).toBe(false);
    });

    /** The view the dataset is read out of; worst verdict wins. */
    it("labels the dataset row with the worst verdict given", async () => {
      await repository.saveReportFeedback({
        requestId: "REQ-1",
        userId: "U1",
        reaction: "white_check_mark",
        label: "correct",
      });
      await repository.saveReportFeedback({
        requestId: "REQ-1",
        userId: "U2",
        reaction: "x",
        label: "incorrect",
      });
      const row = await pool.query(
        "SELECT label FROM aiops_labeled_dataset WHERE request_id = $1",
        ["REQ-1"],
      );
      expect(row.rows[0].label).toBe("incorrect");
    });
  });

  describe("the template registry", () => {
    it("creates a template at version 1", async () => {
      const saved = await repository.saveTemplate("incident_rca", TEMPLATE);
      expect(saved).toEqual({ version: 1, changed: true, created: true });
    });

    /**
     * Comparison happens in SQL because jsonb equality is semantic: a round
     * trip that reorders keys is not a change, and comparing serialised
     * objects in JS reports one on every write.
     */
    it("does not bump the version when nothing actually differs", async () => {
      await repository.saveTemplate("incident_rca", TEMPLATE);
      const again = await repository.saveTemplate("incident_rca", {
        ...(TEMPLATE as object),
        output: { sections: [{ heading: "요약", id: "summary" }] },
      } as never);
      expect(again).toEqual({ version: 1, changed: false, created: false });
    });

    it("bumps the version when the content really changes", async () => {
      await repository.saveTemplate("incident_rca", TEMPLATE);
      const changed = await repository.saveTemplate("incident_rca", {
        ...(TEMPLATE as object),
        title: "사고 RCA 보고서",
      } as never);
      expect(changed).toEqual({ version: 2, changed: true, created: false });
    });

    /**
     * Versions continue from the history, not from the live row. Deleting a
     * template and adding it back is ordinary, and restarting at 1 would put
     * two different templates under one (id, version) -- which is what a
     * stored report cites when it says what shaped it.
     */
    it("continues numbering after a template is deleted and re-added", async () => {
      await repository.saveTemplate("incident_rca", TEMPLATE);
      await repository.saveTemplate("incident_rca", {
        ...(TEMPLATE as object),
        title: "두 번째",
      } as never);
      expect(await repository.deleteTemplate("incident_rca")).toBe(true);

      const revived = await repository.saveTemplate("incident_rca", TEMPLATE);
      expect(revived.version).toBe(3);
      expect(revived.created).toBe(true);
    });

    it("keeps every version it ever had, even after a delete", async () => {
      await repository.saveTemplate("incident_rca", TEMPLATE);
      await repository.deleteTemplate("incident_rca");
      const history = await pool.query(
        "SELECT version FROM aiops_report_template_versions WHERE template_id = $1",
        ["incident_rca"],
      );
      expect(history.rowCount).toBe(1);
    });

    it("hides a disabled template from the catalog unless asked", async () => {
      await repository.saveTemplate("incident_rca", TEMPLATE);
      await repository.saveTemplate("log_review", {
        ...(TEMPLATE as object),
        enabled: false,
      } as never);

      expect((await repository.listTemplates(false)).map((t) => t.template_id)).toEqual([
        "incident_rca",
      ]);
      expect((await repository.listTemplates(true)).map((t) => t.template_id)).toEqual([
        "incident_rca",
        "log_review",
      ]);
    });

    it("says nothing was deleted when there was nothing there", async () => {
      expect(await repository.deleteTemplate("never_existed")).toBe(false);
    });
  });
});
