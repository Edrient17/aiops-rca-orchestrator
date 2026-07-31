import express, { type NextFunction, type Request, type Response } from "express";
import { z } from "zod";
import type { AppConfig } from "./config.js";
import {
  isSlackEventEnvelope,
  makeRequestId,
  stripBotMention,
  verifySlackSignature,
} from "./slack.js";
import type { RequestRepository } from "./types.js";

export interface AppDependencies {
  config: AppConfig;
  repository: RequestRepository;
  onRequestAccepted?: () => void;
}

const statusSchema = z.object({
  status: z.string().min(1).max(100),
  error: z.string().max(4_000).optional(),
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
      );
      response.status(updated ? 200 : 404).json({ updated });
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

function internalAuth(token: string) {
  return (request: Request, response: Response, next: NextFunction): void => {
    if (getHeader(request, "x-aiops-internal-token") !== token) {
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
