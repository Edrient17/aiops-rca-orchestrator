import type { Pool, PoolClient } from "pg";
import { SETTLED_STATUSES } from "./request-status.js";
import type {
  ReportTemplate,
  ReportTemplateBody,
  SaveTemplateResult,
} from "./templates.js";
import type {
  AcceptedSlackRequest,
  AgentRunInput,
  DispatchJob,
  PendingClarification,
  PublishedReport,
  ReportFeedbackInput,
  ReportInput,
  ReportNoteInput,
  ReportRef,
  RequestRepository,
  SaveFeedbackResult,
  SaveRequestResult,
  SystemErrorInput,
} from "./types.js";

export class PostgresRequestRepository implements RequestRepository {
  constructor(private readonly pool: Pool) {}

  async ping(): Promise<void> {
    await this.pool.query("SELECT 1");
  }

  async listTemplates(includeDisabled: boolean): Promise<ReportTemplate[]> {
    const result = await this.pool.query<TemplateRow>(
      `
        SELECT template_id, version, enabled, title, description, collection, output
        FROM aiops_report_templates
        WHERE $1::boolean OR enabled
        ORDER BY template_id
      `,
      [includeDisabled],
    );
    return result.rows.map(toTemplate);
  }

  async getTemplate(templateId: string): Promise<ReportTemplate | null> {
    const result = await this.pool.query<TemplateRow>(
      `
        SELECT template_id, version, enabled, title, description, collection, output
        FROM aiops_report_templates
        WHERE template_id = $1
      `,
      [templateId],
    );
    const row = result.rows[0];
    return row ? toTemplate(row) : null;
  }

  async saveTemplate(
    templateId: string,
    body: ReportTemplateBody,
  ): Promise<SaveTemplateResult> {
    const client = await this.pool.connect();
    const values = [
      templateId,
      body.enabled,
      body.title,
      body.description,
      JSON.stringify(body.collection),
      JSON.stringify(body.output),
    ];
    try {
      await client.query("BEGIN");
      // Comparison happens in SQL rather than in JS because jsonb equality is
      // semantic: Postgres does not care that a round trip reordered the keys,
      // and comparing serialised objects here would report a change on every
      // write. FOR UPDATE so two operators saving at once cannot both read the
      // same version and produce it twice.
      const current = await client.query<{ version: number; unchanged: boolean }>(
        `
          SELECT
            version,
            (enabled, title, description, collection, output)
              IS NOT DISTINCT FROM ($2::boolean, $3, $4, $5::jsonb, $6::jsonb)
              AS unchanged
          FROM aiops_report_templates
          WHERE template_id = $1
          FOR UPDATE
        `,
        values,
      );

      const existing = current.rows[0];
      if (existing?.unchanged) {
        await client.query("COMMIT");
        return { version: existing.version, changed: false, created: false };
      }

      // Versions continue from the history, not from the live row. Deleting a
      // template and adding it back is an ordinary thing to do, and restarting
      // at 1 would collide with the version already recorded under that number
      // -- leaving two different templates sharing one (id, version), which is
      // exactly what a stored report cites when it says what shaped it.
      const version = existing
        ? existing.version + 1
        : ((
            await client.query<{ next: number }>(
              `
                SELECT COALESCE(max(version), 0) + 1 AS next
                FROM aiops_report_template_versions
                WHERE template_id = $1
              `,
              [templateId],
            )
          ).rows[0]?.next ?? 1);
      await client.query(
        `
          INSERT INTO aiops_report_templates (
            template_id, version, enabled, title, description, collection, output
          )
          VALUES ($1, $7, $2, $3, $4, $5::jsonb, $6::jsonb)
          ON CONFLICT (template_id) DO UPDATE
          SET version = EXCLUDED.version,
              enabled = EXCLUDED.enabled,
              title = EXCLUDED.title,
              description = EXCLUDED.description,
              collection = EXCLUDED.collection,
              output = EXCLUDED.output,
              updated_at = now()
        `,
        [...values, version],
      );
      await client.query(
        `
          INSERT INTO aiops_report_template_versions (
            template_id, version, enabled, title, description, collection, output
          )
          VALUES ($1, $7, $2, $3, $4, $5::jsonb, $6::jsonb)
          ON CONFLICT (template_id, version) DO NOTHING
        `,
        [...values, version],
      );
      await client.query("COMMIT");
      return { version, changed: true, created: !existing };
    } catch (error) {
      await safeRollback(client);
      throw error;
    } finally {
      client.release();
    }
  }

  async deleteTemplate(templateId: string): Promise<boolean> {
    // Only the live row goes. aiops_report_template_versions keeps the history,
    // which is what a report published under this template still refers to.
    const result = await this.pool.query(
      `DELETE FROM aiops_report_templates WHERE template_id = $1`,
      [templateId],
    );
    return result.rowCount === 1;
  }

  async saveSlackRequest(request: AcceptedSlackRequest): Promise<SaveRequestResult> {
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      const inserted = await client.query<{ request_id: string }>(
        `
          INSERT INTO aiops_requests (
            request_id,
            slack_event_id,
            team_id,
            channel_id,
            user_id,
            message_ts,
            thread_ts,
            question,
            received_at,
            raw_payload,
            parent_request_id
          )
          VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)
          -- Untargeted so it catches both duplicate keys. A redelivery repeats
          -- the event id, but an app subscribed to app_mention and
          -- message.channels receives one mention as two events with different
          -- ids and the same message, which only the message key can catch.
          ON CONFLICT DO NOTHING
          RETURNING request_id
        `,
        [
          request.requestId,
          request.slackEventId,
          request.teamId,
          request.channelId,
          request.userId,
          request.messageTs,
          request.threadTs,
          request.question,
          request.receivedAt,
          JSON.stringify(request.rawPayload),
          request.parentRequestId ?? null,
        ],
      );

      if (inserted.rowCount === 1) {
        await client.query(
          `
            INSERT INTO aiops_dispatch_queue (request_id)
            VALUES ($1)
            ON CONFLICT (request_id) DO NOTHING
          `,
          [request.requestId],
        );
        if (request.parentRequestId) {
          // The parent has its answer now, so it should stop showing up as
          // still waiting on the user.
          await client.query(
            `
              UPDATE aiops_requests
              SET status = 'clarified', updated_at = now()
              WHERE request_id = $1 AND status = 'needs_clarification'
            `,
            [request.parentRequestId],
          );
        }
        await client.query("COMMIT");
        return { created: true, requestId: request.requestId };
      }

      const existing = await client.query<{ request_id: string }>(
        `
          SELECT request_id
          FROM aiops_requests
          WHERE slack_event_id = $1
             OR (channel_id = $2 AND message_ts = $3)
          ORDER BY received_at
          LIMIT 1
        `,
        [request.slackEventId, request.channelId, request.messageTs],
      );
      await client.query("COMMIT");
      return {
        created: false,
        requestId: existing.rows[0]?.request_id ?? request.requestId,
      };
    } catch (error) {
      await safeRollback(client);
      throw error;
    } finally {
      client.release();
    }
  }

  async findPendingClarification(
    channelId: string,
    threadTs: string,
  ): Promise<PendingClarification | null> {
    // A reply in a thread carries the parent message's ts as thread_ts, so the
    // request awaiting an answer is the one whose own message_ts matches.
    const result = await this.pool.query<{
      request_id: string;
      question: string;
    }>(
      `
        SELECT request_id, question
        FROM aiops_requests
        WHERE channel_id = $1
          AND message_ts = $2
          AND status = 'needs_clarification'
        ORDER BY received_at DESC
        LIMIT 1
      `,
      [channelId, threadTs],
    );

    const row = result.rows[0];
    return row ? { requestId: row.request_id, question: row.question } : null;
  }

  async claimDispatch(lockSeconds: number): Promise<DispatchJob | null> {
    const result = await this.pool.query<{
      id: string;
      request_id: string;
      attempts: number;
      slack_event_id: string;
      team_id: string | null;
      channel_id: string;
      user_id: string;
      message_ts: string;
      thread_ts: string | null;
      question: string;
      received_at: Date;
      parent_request_id: string | null;
      prior_question: string | null;
      parent_ack_ts: string | null;
      slack_ack_ts: string | null;
    }>(
      `
        WITH candidate AS (
          SELECT id
          FROM aiops_dispatch_queue
          WHERE dispatched_at IS NULL
            AND next_attempt_at <= now()
            AND (locked_until IS NULL OR locked_until < now())
          ORDER BY created_at
          FOR UPDATE SKIP LOCKED
          LIMIT 1
        )
        UPDATE aiops_dispatch_queue AS queue
        SET attempts = queue.attempts + 1,
            locked_until = now() + ($1 * interval '1 second'),
            updated_at = now()
        FROM candidate, aiops_requests AS request
        WHERE queue.id = candidate.id
          AND request.request_id = queue.request_id
        RETURNING
          queue.id,
          queue.request_id,
          queue.attempts,
          request.slack_event_id,
          request.team_id,
          request.channel_id,
          request.user_id,
          request.message_ts,
          request.thread_ts,
          request.question,
          request.received_at,
          request.parent_request_id,
          -- Set by the first attempt. A retry reuses it rather than
          -- posting a second acknowledgement for the same question.
          request.slack_ack_ts,
          (
            SELECT parent.question
            FROM aiops_requests AS parent
            WHERE parent.request_id = request.parent_request_id
          ) AS prior_question,
          (
            SELECT parent.slack_ack_ts
            FROM aiops_requests AS parent
            WHERE parent.request_id = request.parent_request_id
          ) AS parent_ack_ts
      `,
      [lockSeconds],
    );

    const row = result.rows[0];
    if (!row) {
      return null;
    }

    return {
      id: Number(row.id),
      requestId: row.request_id,
      attempts: row.attempts,
      payload: {
        request_id: row.request_id,
        slack_event_id: row.slack_event_id,
        team_id: row.team_id,
        channel_id: row.channel_id,
        user_id: row.user_id,
        message_ts: row.message_ts,
        thread_ts: row.thread_ts,
        question: row.question,
        received_at: row.received_at.toISOString(),
        parent_request_id: row.parent_request_id,
        prior_question: row.prior_question,
        parent_ack_ts: row.parent_ack_ts,
        slack_ack_ts: row.slack_ack_ts,
      },
    };
  }

  async completeDispatch(jobId: number): Promise<void> {
    await this.pool.query(
      `
        UPDATE aiops_dispatch_queue
        SET dispatched_at = now(),
            locked_until = NULL,
            last_error = NULL,
            updated_at = now()
        WHERE id = $1
      `,
      [jobId],
    );
  }

  async retryDispatch(jobId: number, delaySeconds: number, error: string): Promise<void> {
    await this.pool.query(
      `
        UPDATE aiops_dispatch_queue
        SET next_attempt_at = now() + ($2 * interval '1 second'),
            locked_until = NULL,
            last_error = $3,
            updated_at = now()
        WHERE id = $1
      `,
      [jobId, delaySeconds, error.slice(0, 4_000)],
    );
  }

  async findRequestStatus(requestId: string): Promise<string | null> {
    // request_id is the primary key, so there is one row at most and nothing
    // to order or narrow -- unlike findPendingClarification, which matches on
    // a Slack thread and has to pick.
    const result = await this.pool.query<{ status: string }>(
      `
        SELECT status
        FROM aiops_requests
        WHERE request_id = $1
      `,
      [requestId],
    );

    return result.rows[0]?.status ?? null;
  }

  async updateRequestStatus(
    requestId: string,
    status: string,
    error?: string,
    slackAckTs?: string,
  ): Promise<boolean> {
    // COALESCE so a later status change cannot erase the anchor recorded when
    // the acknowledgement was first posted.
    //
    // A write of 'failed' is refused for a request that already reached an
    // outcome the asker saw. It is the same rule recordSystemError applies, on
    // the same list, because it is the same write arriving by the other route:
    // the dispatcher marks a request failed when it gives up, and giving up
    // after a delivery that succeeded would retract a report or a
    // clarification that is already in the channel. Only 'failed' is gated --
    // the pipeline's own writes move a request through its statuses and have
    // to land.
    const result = await this.pool.query(
      `
        UPDATE aiops_requests
        SET status = $2,
            last_error = $3,
            slack_ack_ts = COALESCE($4, slack_ack_ts),
            updated_at = now()
        WHERE request_id = $1
          AND ($2 <> 'failed' OR status <> ALL($5::text[]))
      `,
      [requestId, status, error ?? null, slackAckTs ?? null, [...SETTLED_STATUSES]],
    );
    return result.rowCount === 1;
  }

  async recordAgentRun(requestId: string, input: AgentRunInput): Promise<boolean> {
    const result = await this.pool.query(
      `
        INSERT INTO aiops_agent_runs (
          request_id,
          stage,
          status,
          model,
          duration_ms,
          output,
          error
        )
        SELECT $1, $2, $3, $4, $5, $6::jsonb, $7
        WHERE EXISTS (
          SELECT 1 FROM aiops_requests WHERE request_id = $1
        )
      `,
      [
        requestId,
        input.stage,
        input.status,
        input.model ?? null,
        input.durationMs ?? null,
        JSON.stringify(input.output ?? null),
        input.error ?? null,
      ],
    );
    return result.rowCount === 1;
  }

  async saveReport(requestId: string, input: ReportInput): Promise<boolean> {
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      const result = await client.query(
        `
          INSERT INTO aiops_reports (
            request_id,
            parsed_request,
            evidence_package,
            rca_report,
            slack_markdown,
            slack_channel_id,
            slack_message_ts
          )
          SELECT $1, $2::jsonb, $3::jsonb, $4::jsonb, $5, $6, $7
          WHERE EXISTS (
            SELECT 1 FROM aiops_requests WHERE request_id = $1
          )
          ON CONFLICT (request_id) DO UPDATE
          SET parsed_request = EXCLUDED.parsed_request,
              evidence_package = EXCLUDED.evidence_package,
              rca_report = EXCLUDED.rca_report,
              slack_markdown = EXCLUDED.slack_markdown,
              slack_channel_id = EXCLUDED.slack_channel_id,
              slack_message_ts = EXCLUDED.slack_message_ts,
              updated_at = now()
        `,
        [
          requestId,
          JSON.stringify(input.parsedRequest),
          JSON.stringify(input.evidencePackage),
          JSON.stringify(input.rcaReport),
          input.slackMarkdown,
          input.slackChannelId,
          input.slackMessageTs ?? null,
        ],
      );

      if (result.rowCount !== 1) {
        await client.query("ROLLBACK");
        return false;
      }

      await insertToolCalls(client, requestId, input.evidencePackage);
      await client.query(
        `
          UPDATE aiops_requests
          SET status = 'completed',
              last_error = NULL,
              completed_at = now(),
              updated_at = now()
          WHERE request_id = $1
        `,
        [requestId],
      );
      await client.query("COMMIT");
      return true;
    } catch (error) {
      await safeRollback(client);
      throw error;
    } finally {
      client.release();
    }
  }

  async recordSystemError(input: SystemErrorInput): Promise<void> {
    const requestId = input.requestId ?? null;

    // execution_id, last_node and details are left unwritten. They belonged to
    // the n8n error workflow, which reported a failure it could only name by
    // its own execution; the dispatcher that replaced it is inside the process
    // that failed and already knows the request. The columns stay because the
    // rows that workflow wrote still carry them.
    await this.pool.query(
      `
        INSERT INTO aiops_system_errors (request_id, workflow_name, message)
        VALUES ($1, $2, $3)
      `,
      [requestId, input.workflowName ?? null, input.message],
    );

    if (requestId) {
      // Guarded rather than unconditional: the dispatcher gives up after its
      // last attempt, and a report delivered by an earlier one must not be
      // retracted by the attempt that came after it.
      //
      // The guard used to name 'completed' alone, and a clarification was left
      // exposed to the case it was written for. The dispatcher records a
      // failure to close a delivered job against that job's request, and the
      // delivery it could not close may be one that asked the asker a question
      // -- so this relabelled a request that had just been sent a
      // clarification as failed, which cost two things. The asker's reply
      // stopped matching: findPendingClarification looks for
      // 'needs_clarification' and found nothing, so the answer they typed
      // opened no continuation. And the status stopped being usable as the
      // record that the question went out, which is what runInvestigation
      // reads to keep from asking it twice.
      await this.pool.query(
        `
          UPDATE aiops_requests
          SET status = 'failed', last_error = $2, updated_at = now()
          WHERE request_id = $1 AND status <> ALL($3::text[])
        `,
        [requestId, input.message, [...SETTLED_STATUSES]],
      );
    }
  }

  async findReportByRequest(requestId: string): Promise<PublishedReport | null> {
    // request_id is the primary key here, so there is one row at most and
    // nothing to order or narrow -- unlike the two lookups below, which match
    // on a Slack message and have to pick.
    const result = await this.pool.query<{
      slack_channel_id: string;
      slack_message_ts: string | null;
    }>(
      `
        SELECT slack_channel_id, slack_message_ts
        FROM aiops_reports
        WHERE request_id = $1
      `,
      [requestId],
    );

    const row = result.rows[0];
    return row
      ? { channelId: row.slack_channel_id, messageTs: row.slack_message_ts }
      : null;
  }

  async findReportByMessage(
    channelId: string,
    messageTs: string,
  ): Promise<ReportRef | null> {
    return this.findReport(
      `report.slack_channel_id = $1 AND report.slack_message_ts = $2`,
      channelId,
      messageTs,
    );
  }

  async findReportByThread(
    channelId: string,
    threadTs: string,
  ): Promise<ReportRef | null> {
    // The report is a reply under the acknowledgement, so a reply to it carries
    // the acknowledgement ts. Matching the report ts too covers the case where
    // the report started its own thread.
    return this.findReport(
      `report.slack_channel_id = $1
         AND (request.slack_ack_ts = $2 OR report.slack_message_ts = $2)`,
      channelId,
      threadTs,
    );
  }

  private async findReport(
    predicate: string,
    channelId: string,
    ts: string,
  ): Promise<ReportRef | null> {
    const result = await this.pool.query<{
      request_id: string;
      thread_ts: string;
    }>(
      `
        SELECT
          report.request_id,
          COALESCE(request.slack_ack_ts, report.slack_message_ts) AS thread_ts
        FROM aiops_reports AS report
        JOIN aiops_requests AS request USING (request_id)
        WHERE ${predicate}
        ORDER BY report.created_at DESC
        LIMIT 1
      `,
      [channelId, ts],
    );

    const row = result.rows[0];
    return row ? { requestId: row.request_id, threadTs: row.thread_ts } : null;
  }

  async saveReportFeedback(input: ReportFeedbackInput): Promise<SaveFeedbackResult> {
    const inserted = await this.pool.query(
      `
        INSERT INTO aiops_report_feedback (request_id, user_id, reaction, label)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (request_id, user_id, reaction) DO NOTHING
      `,
      [input.requestId, input.userId, input.reaction, input.label],
    );

    const created = inserted.rowCount === 1;
    if (!created || input.label === "correct") {
      return { created, shouldAskForCorrection: false };
    }

    // Ask once per report: only when this is the first negative verdict and
    // nobody has written the correction yet.
    const state = await this.pool.query<{ negatives: string; notes: string }>(
      `
        SELECT
          (
            SELECT count(*) FROM aiops_report_feedback
            WHERE request_id = $1 AND label <> 'correct'
          ) AS negatives,
          (
            SELECT count(*) FROM aiops_report_notes WHERE request_id = $1
          ) AS notes
      `,
      [input.requestId],
    );

    const row = state.rows[0];
    return {
      created,
      shouldAskForCorrection: Number(row?.negatives) === 1 && Number(row?.notes) === 0,
    };
  }

  async removeReportFeedback(input: {
    requestId: string;
    userId: string;
    reaction: string;
  }): Promise<boolean> {
    const result = await this.pool.query(
      `
        DELETE FROM aiops_report_feedback
        WHERE request_id = $1 AND user_id = $2 AND reaction = $3
      `,
      [input.requestId, input.userId, input.reaction],
    );
    return result.rowCount === 1;
  }

  async saveReportNote(input: ReportNoteInput): Promise<boolean> {
    const result = await this.pool.query(
      `
        INSERT INTO aiops_report_notes (request_id, user_id, slack_message_ts, note)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (request_id, slack_message_ts) DO NOTHING
      `,
      [input.requestId, input.userId, input.slackMessageTs, input.note],
    );
    return result.rowCount === 1;
  }

  async getRequest(requestId: string): Promise<unknown | null> {
    const result = await this.pool.query(
      `
        SELECT
          request.request_id,
          request.slack_event_id,
          request.team_id,
          request.channel_id,
          request.user_id,
          request.message_ts,
          request.thread_ts,
          request.question,
          request.status,
          request.last_error,
          request.received_at,
          request.completed_at,
          report.rca_report,
          report.slack_channel_id,
          report.slack_message_ts
        FROM aiops_requests AS request
        LEFT JOIN aiops_reports AS report USING (request_id)
        WHERE request.request_id = $1
      `,
      [requestId],
    );
    return result.rows[0] ?? null;
  }
}

interface TemplateRow {
  template_id: string;
  version: number;
  enabled: boolean;
  title: string;
  description: string;
  collection: unknown;
  output: unknown;
}

function toTemplate(row: TemplateRow): ReportTemplate {
  return {
    template_id: row.template_id,
    version: row.version,
    enabled: row.enabled,
    title: row.title,
    description: row.description,
    collection: row.collection as ReportTemplate["collection"],
    output: row.output as ReportTemplate["output"],
  };
}

async function safeRollback(client: PoolClient): Promise<void> {
  try {
    await client.query("ROLLBACK");
  } catch {
    // Preserve the original database error.
  }
}

async function insertToolCalls(
  client: PoolClient,
  requestId: string,
  evidencePackage: unknown,
): Promise<void> {
  const calls = extractToolCalls(evidencePackage);
  for (const call of calls) {
    await client.query(
      `
        INSERT INTO aiops_tool_calls (
          request_id,
          tool_call_id,
          tool_name,
          purpose,
          status
        )
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (request_id, tool_call_id) DO UPDATE
        SET tool_name = EXCLUDED.tool_name,
            purpose = EXCLUDED.purpose,
            status = EXCLUDED.status
      `,
      [requestId, call.toolCallId, call.toolName, call.purpose, call.status],
    );
  }
}

function extractToolCalls(value: unknown): Array<{
  toolCallId: string;
  toolName: string;
  purpose: string;
  status: string;
}> {
  if (typeof value !== "object" || value === null) {
    return [];
  }
  const investigation = (value as Record<string, unknown>).investigation;
  if (typeof investigation !== "object" || investigation === null) {
    return [];
  }
  const toolCalls = (investigation as Record<string, unknown>).tool_calls;
  if (!Array.isArray(toolCalls)) {
    return [];
  }

  return toolCalls.flatMap((value) => {
    if (typeof value !== "object" || value === null) {
      return [];
    }
    const call = value as Record<string, unknown>;
    if (
      typeof call.tool_call_id !== "string" ||
      typeof call.tool_name !== "string" ||
      typeof call.purpose !== "string" ||
      typeof call.status !== "string"
    ) {
      return [];
    }
    return [
      {
        toolCallId: call.tool_call_id,
        toolName: call.tool_name,
        purpose: call.purpose,
        status: call.status,
      },
    ];
  });
}
