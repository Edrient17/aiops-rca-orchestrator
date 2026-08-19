import { timingSafeEqual } from "node:crypto";
import express, { type NextFunction, type Request, type Response } from "express";
import { z } from "zod";
import type { AppConfig } from "./config.js";
import {
  isSlackEventEnvelope,
  makeRequestId,
  normalizeReaction,
  postThreadReply,
  stripBotMention,
  verifySlackSignature,
} from "./slack.js";
import { reportTemplateSchema, templateIdSchema } from "./templates.js";
import type { RequestRepository, SlackEventEnvelope } from "./types.js";

type SlackEvent = SlackEventEnvelope["event"];

const CORRECTION_PROMPT =
  "이 보고서의 판단이 실제와 달랐다고 표시되었습니다. " +
  "실제 원인이 무엇이었는지 이 스레드에 남겨 주시면 함께 기록됩니다.";

export interface AppDependencies {
  config: AppConfig;
  repository: RequestRepository;
  onRequestAccepted?: () => void;
}

const statusSchema = z.object({
  status: z.string().min(1).max(100),
  error: z.string().max(4_000).optional(),
  /** Anchor message in the answer channel, recorded once when first posted. */
  slack_ack_ts: z.string().max(100).optional(),
});

const agentRunSchema = z.object({
  stage: z.enum(["question_analyzer", "evidence_collector", "rca_writer"]),
  status: z.enum(["succeeded", "failed"]),
  model: z.string().max(200).optional(),
  duration_ms: z.number().int().min(0).optional(),
  output: z.unknown().optional(),
  error: z.string().max(10_000).optional(),
});

const reportSchema = z.object({
  parsed_request: z.unknown(),
  evidence_package: z.unknown(),
  rca_report: z.unknown(),
  slack_markdown: z.string().min(1).max(100_000),
  slack_channel_id: z.string().min(1).max(100),
  slack_message_ts: z.string().max(100).optional(),
});

const executionSchema = z.object({
  execution_id: z.string().min(1).max(200),
});

const errorSchema = z.object({
  request_id: z.string().max(200).optional(),
  workflow_name: z.string().max(500).optional(),
  execution_id: z.string().max(200).optional(),
  last_node: z.string().max(500).optional(),
  message: z.string().min(1).max(10_000),
  details: z.unknown().optional(),
});

export function createApp(dependencies: AppDependencies): express.Express {
  const app = express();
  app.disable("x-powered-by");

  app.get("/healthz", (_request, response) => {
    response.json({ status: "ok" });
  });

  app.get("/readyz", async (_request, response, next) => {
    try {
      await dependencies.repository.ping();
      response.json({ status: "ready" });
    } catch (error) {
      next(error);
    }
  });

  app.post(
    "/slack/events",
    express.raw({ type: "application/json", limit: "1mb" }),
    async (request, response, next) => {
      try {
        const rawBody = Buffer.isBuffer(request.body)
          ? request.body
          : Buffer.from(request.body ?? "");
        const validSignature = verifySlackSignature({
          signingSecret: dependencies.config.slackSigningSecret,
          timestamp: getHeader(request, "x-slack-request-timestamp"),
          signature: getHeader(request, "x-slack-signature"),
          rawBody,
        });
        if (!validSignature) {
          response.status(401).json({ error: "invalid_slack_signature" });
          return;
        }

        let payload: unknown;
        try {
          payload = JSON.parse(rawBody.toString("utf8"));
        } catch {
          response.status(400).json({ error: "invalid_json" });
          return;
        }

        if (
          typeof payload === "object" &&
          payload !== null &&
          (payload as Record<string, unknown>).type === "url_verification" &&
          typeof (payload as Record<string, unknown>).challenge === "string"
        ) {
          response.json({ challenge: (payload as Record<string, unknown>).challenge });
          return;
        }

        if (!isSlackEventEnvelope(payload)) {
          response.status(200).json({ ignored: true });
          return;
        }

        if (
          dependencies.config.slackAppId &&
          payload.api_app_id !== dependencies.config.slackAppId
        ) {
          response.status(200).json({ ignored: true });
          return;
        }

        const event = payload.event;

        // A reaction on a published report is a verdict on that investigation.
        // It carries no text and points at its target through event.item, so it
        // cannot go through the question path below.
        if (event.type === "reaction_added" || event.type === "reaction_removed") {
          response.status(200).json(await handleReaction(dependencies, event));
          return;
        }

        // A reply written under a report says what the truth actually was. The
        // question channel is left alone; a reply there answers a clarification.
        if (
          event.type === "message" &&
          event.thread_ts &&
          event.channel &&
          event.channel !== dependencies.config.slackQuestionChannelId
        ) {
          response.status(200).json(await handleReportNote(dependencies, event));
          return;
        }

        const isSupportedType = event.type === "app_mention" || event.type === "message";
        if (
          !isSupportedType ||
          event.channel !== dependencies.config.slackQuestionChannelId ||
          !event.user ||
          !event.ts ||
          !event.text ||
          (dependencies.config.slackAllowedUserIds.size > 0 &&
            !dependencies.config.slackAllowedUserIds.has(event.user)) ||
          Boolean(event.bot_id) ||
          Boolean(event.subtype) ||
          event.user === dependencies.config.slackBotUserId
        ) {
          response.status(200).json({ ignored: true });
          return;
        }

        const question = stripBotMention(event.text, dependencies.config.slackBotUserId);
        if (!question) {
          response.status(200).json({ ignored: true });
          return;
        }

        // A reply inside a thread may be answering a clarification this bot
        // asked for. Linking the two lets the analyzer read the original
        // question and the answer together instead of starting over.
        const parent = event.thread_ts
          ? await dependencies.repository.findPendingClarification(
              event.channel,
              event.thread_ts,
            )
          : null;

        const receivedAt = new Date().toISOString();
        const requestId = makeRequestId(payload.event_id, payload.event_time);
        const result = await dependencies.repository.saveSlackRequest({
          requestId,
          slackEventId: payload.event_id,
          teamId: payload.team_id ?? null,
          channelId: event.channel,
          userId: event.user,
          messageTs: event.ts,
          threadTs: event.thread_ts ?? null,
          question,
          receivedAt,
          rawPayload: payload,
          parentRequestId: parent?.requestId ?? null,
        });

        response.status(200).json({
          accepted: true,
          duplicate: !result.created,
          request_id: result.requestId,
          ...(parent ? { clarifies: parent.requestId } : {}),
        });

        if (result.created) {
          dependencies.onRequestAccepted?.();
        }
      } catch (error) {
        next(error);
      }
    },
  );

  app.use(express.json({ limit: "2mb" }));
  app.use("/internal", internalAuth(dependencies.config.internalToken));

  // The catalog the question analyzer classifies against, and the CRUD an
  // operator uses to grow it. Both sit behind the internal token: these are not
  // reachable through the reverse proxy, so an operator edits them from the
  // host, which is the same boundary the rest of /internal already assumes.
  app.get("/internal/templates", async (request, response, next) => {
    try {
      const includeDisabled = request.query.all === "true";
      response.json({
        templates: await dependencies.repository.listTemplates(includeDisabled),
      });
    } catch (error) {
      next(error);
    }
  });

  app.get("/internal/templates/:templateId", async (request, response, next) => {
    try {
      const template = await dependencies.repository.getTemplate(
        request.params.templateId,
      );
      if (!template) {
        response.status(404).json({ error: "template_not_found" });
        return;
      }
      response.json(template);
    } catch (error) {
      next(error);
    }
  });

  // Validated here rather than where it is used. A template becomes prompt text
  // for an agent midway through an investigation, so a malformed one would
  // otherwise fail a question that had already been accepted and acknowledged.
  app.put("/internal/templates/:templateId", async (request, response, next) => {
    try {
      const templateId = templateIdSchema.parse(request.params.templateId);
      const body = reportTemplateSchema.parse(request.body);
      const result = await dependencies.repository.saveTemplate(templateId, body);
      response.status(result.created ? 201 : 200).json({
        template_id: templateId,
        ...result,
      });
    } catch (error) {
      next(error);
    }
  });

  app.delete("/internal/templates/:templateId", async (request, response, next) => {
    try {
      const deleted = await dependencies.repository.deleteTemplate(
        request.params.templateId,
      );
      response.status(deleted ? 200 : 404).json({ deleted });
    } catch (error) {
      next(error);
    }
  });

  app.get("/internal/requests/:requestId", async (request, response, next) => {
    try {
      const value = await dependencies.repository.getRequest(request.params.requestId);
      if (!value) {
        response.status(404).json({ error: "request_not_found" });
        return;
      }
      response.json(value);
    } catch (error) {
      next(error);
    }
  });

  app.post("/internal/requests/:requestId/status", async (request, response, next) => {
    try {
      const body = statusSchema.parse(request.body);
      const updated = await dependencies.repository.updateRequestStatus(
        request.params.requestId,
        body.status,
        body.error,
        body.slack_ack_ts,
      );
      response.status(updated ? 200 : 404).json({ updated });
    } catch (error) {
      next(error);
    }
  });

  // Called before the run does anything that can fail. A failure afterwards is
  // reported by the error workflow, which knows only its execution id, and this
  // is the mapping that turns that back into a request.
  app.post("/internal/requests/:requestId/execution", async (request, response, next) => {
    try {
      const body = executionSchema.parse(request.body);
      const linked = await dependencies.repository.setExecutionId(
        request.params.requestId,
        body.execution_id,
      );
      response.status(linked ? 200 : 404).json({ linked });
    } catch (error) {
      next(error);
    }
  });

  app.post("/internal/requests/:requestId/agent-runs", async (request, response, next) => {
    try {
      const body = agentRunSchema.parse(request.body);
      const created = await dependencies.repository.recordAgentRun(
        request.params.requestId,
        {
          stage: body.stage,
          status: body.status,
          ...(body.model ? { model: body.model } : {}),
          ...(body.duration_ms !== undefined ? { durationMs: body.duration_ms } : {}),
          ...(body.output !== undefined ? { output: body.output } : {}),
          ...(body.error ? { error: body.error } : {}),
        },
      );
      response.status(created ? 201 : 404).json({ created });
    } catch (error) {
      next(error);
    }
  });

  app.put("/internal/requests/:requestId/report", async (request, response, next) => {
    try {
      const body = reportSchema.parse(request.body);
      const saved = await dependencies.repository.saveReport(request.params.requestId, {
        parsedRequest: body.parsed_request,
        evidencePackage: body.evidence_package,
        rcaReport: body.rca_report,
        slackMarkdown: body.slack_markdown,
        slackChannelId: body.slack_channel_id,
        ...(body.slack_message_ts ? { slackMessageTs: body.slack_message_ts } : {}),
      });
      response.status(saved ? 200 : 404).json({ saved });
    } catch (error) {
      next(error);
    }
  });

  app.post("/internal/errors", async (request, response, next) => {
    try {
      const body = errorSchema.parse(request.body);
      await dependencies.repository.recordSystemError({
        ...(body.request_id ? { requestId: body.request_id } : {}),
        ...(body.workflow_name ? { workflowName: body.workflow_name } : {}),
        ...(body.execution_id ? { executionId: body.execution_id } : {}),
        ...(body.last_node ? { lastNode: body.last_node } : {}),
        message: body.message,
        ...(body.details !== undefined ? { details: body.details } : {}),
      });
      response.status(201).json({ created: true });
    } catch (error) {
      next(error);
    }
  });

  app.use(
    (
      error: unknown,
      _request: Request,
      response: Response,
      _next: NextFunction,
    ): void => {
      if (error instanceof z.ZodError) {
        response.status(400).json({
          error: "invalid_request",
          issues: error.issues,
        });
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      console.error(JSON.stringify({ level: "error", message }));
      response.status(500).json({ error: "internal_server_error" });
    },
  );

  return app;
}

/**
 * Turns a reaction on a published report into a stored verdict. Anything that
 * is not a known emoji on a known report is ignored rather than rejected, so
 * ordinary channel activity costs at most one lookup.
 */
async function handleReaction(
  dependencies: AppDependencies,
  event: SlackEvent,
): Promise<Record<string, unknown>> {
  const { config, repository } = dependencies;
  const reaction = normalizeReaction(event.reaction ?? "");
  const label = config.labelReactions.get(reaction);
  const channel = event.item?.channel;
  const messageTs = event.item?.ts;

  if (
    !label ||
    !event.user ||
    !channel ||
    !messageTs ||
    event.item?.type !== "message" ||
    event.user === config.slackBotUserId ||
    (config.slackAllowedUserIds.size > 0 && !config.slackAllowedUserIds.has(event.user))
  ) {
    return { ignored: true };
  }

  const report = await repository.findReportByMessage(channel, messageTs);
  if (!report) {
    return { ignored: true };
  }

  if (event.type === "reaction_removed") {
    const removed = await repository.removeReportFeedback({
      requestId: report.requestId,
      userId: event.user,
      reaction,
    });
    return { request_id: report.requestId, label, removed };
  }

  const result = await repository.saveReportFeedback({
    requestId: report.requestId,
    userId: event.user,
    reaction,
    label,
  });

  if (result.shouldAskForCorrection) {
    askForCorrection(dependencies, channel, report.threadTs);
  }

  return { request_id: report.requestId, label, labeled: result.created };
}

/**
 * Stores a reply written under a report as the correction the reaction could
 * not carry.
 */
async function handleReportNote(
  dependencies: AppDependencies,
  event: SlackEvent,
): Promise<Record<string, unknown>> {
  const { config, repository } = dependencies;
  const channel = event.channel;
  const threadTs = event.thread_ts;
  const note = stripBotMention(event.text ?? "", config.slackBotUserId);

  if (
    !channel ||
    !threadTs ||
    !event.user ||
    !event.ts ||
    !note ||
    Boolean(event.bot_id) ||
    Boolean(event.subtype) ||
    event.user === config.slackBotUserId ||
    (config.slackAllowedUserIds.size > 0 && !config.slackAllowedUserIds.has(event.user))
  ) {
    return { ignored: true };
  }

  const report = await repository.findReportByThread(channel, threadTs);
  if (!report) {
    return { ignored: true };
  }

  const created = await repository.saveReportNote({
    requestId: report.requestId,
    userId: event.user,
    slackMessageTs: event.ts,
    note: note.slice(0, 10_000),
  });
  return { request_id: report.requestId, note_recorded: created };
}

/**
 * Slack has already been acknowledged by the time this runs, so a failure is
 * logged and dropped. Retrying would only re-deliver the event.
 */
function askForCorrection(
  dependencies: AppDependencies,
  channel: string,
  threadTs: string,
): void {
  const botToken = dependencies.config.slackBotToken;
  if (!botToken) {
    return;
  }
  void postThreadReply({
    botToken,
    channel,
    threadTs,
    text: CORRECTION_PROMPT,
  }).catch((error: unknown) => {
    console.error(
      JSON.stringify({
        level: "warn",
        message: "correction_prompt_failed",
        detail: error instanceof Error ? error.message : String(error),
      }),
    );
  });
}

function internalAuth(token: string) {
  const expected = Buffer.from(token, "utf8");
  return (request: Request, response: Response, next: NextFunction): void => {
    // Compared in constant time, as the Slack signature already is. A plain
    // !== leaks how much of the token was right through how long the answer
    // took, one byte at a time. These routes sit on the Docker network rather
    // than the internet, which lowers the odds without changing the shape of
    // the mistake.
    const presented = Buffer.from(getHeader(request, "x-aiops-internal-token") ?? "", "utf8");
    if (
      presented.length !== expected.length ||
      !timingSafeEqual(presented, expected)
    ) {
      response.status(401).json({ error: "unauthorized" });
      return;
    }
    next();
  };
}

function getHeader(request: Request, name: string): string | undefined {
  const value = request.headers[name];
  return Array.isArray(value) ? value[0] : value;
}
