import { z } from "zod";

const envSchema = z.object({
  PORT: z.coerce.number().int().min(1).max(65535).default(8080),
  DATABASE_URL: z.string().min(1),
  SLACK_SIGNING_SECRET: z.string().min(1),
  SLACK_QUESTION_CHANNEL_ID: z.string().min(1),
  SLACK_BOT_USER_ID: z.string().optional(),
  SLACK_APP_ID: z.string().optional(),
  SLACK_ALLOWED_USER_IDS: z.string().optional(),
  AIOPS_INTERNAL_TOKEN: z.string().min(24),
  N8N_INTERNAL_WEBHOOK_URL: z.string().url(),
  DISPATCH_INTERVAL_MS: z.coerce.number().int().min(100).max(60_000).default(1_000),
  DISPATCH_TIMEOUT_MS: z.coerce.number().int().min(1_000).max(60_000).default(10_000),
});

export interface AppConfig {
  port: number;
  databaseUrl: string;
  slackSigningSecret: string;
  slackQuestionChannelId: string;
  slackBotUserId?: string;
  slackAppId?: string;
  slackAllowedUserIds: ReadonlySet<string>;
  internalToken: string;
  n8nWebhookUrl: string;
  dispatchIntervalMs: number;
  dispatchTimeoutMs: number;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const parsed = envSchema.parse(env);
  const allowedUsers = new Set(
    (parsed.SLACK_ALLOWED_USER_IDS ?? "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean),
  );

  return {
    port: parsed.PORT,
    databaseUrl: parsed.DATABASE_URL,
    slackSigningSecret: parsed.SLACK_SIGNING_SECRET,
    slackQuestionChannelId: parsed.SLACK_QUESTION_CHANNEL_ID,
    ...(parsed.SLACK_BOT_USER_ID ? { slackBotUserId: parsed.SLACK_BOT_USER_ID } : {}),
    ...(parsed.SLACK_APP_ID ? { slackAppId: parsed.SLACK_APP_ID } : {}),
    slackAllowedUserIds: allowedUsers,
    internalToken: parsed.AIOPS_INTERNAL_TOKEN,
    n8nWebhookUrl: parsed.N8N_INTERNAL_WEBHOOK_URL,
    dispatchIntervalMs: parsed.DISPATCH_INTERVAL_MS,
    dispatchTimeoutMs: parsed.DISPATCH_TIMEOUT_MS,
  };
}
