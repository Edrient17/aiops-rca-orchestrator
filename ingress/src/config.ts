import { z } from "zod";
import type { FeedbackLabel } from "./types.js";

const FEEDBACK_LABELS: readonly FeedbackLabel[] = ["correct", "partial", "incorrect"];

/**
 * Exported so a test can read back which variables this service refuses to
 * start without, and check them against what docker-compose.yml actually
 * guarantees. See tests/config.test.ts.
 */
export const envSchema = z.object({
  PORT: z.coerce.number().int().min(1).max(65535).default(8080),
  DATABASE_URL: z.string().min(1),
  SLACK_SIGNING_SECRET: z.string().min(1),
  SLACK_QUESTION_CHANNEL_ID: z.string().min(1),
  SLACK_BOT_USER_ID: z.string().optional(),
  SLACK_APP_ID: z.string().optional(),
  SLACK_ALLOWED_USER_IDS: z.string().optional(),
  AIOPS_INTERNAL_TOKEN: z.string().min(24),
  SLACK_BOT_TOKEN: z.string().min(1),
  SLACK_LABEL_REACTIONS: z
    .string()
    .default(
      "white_check_mark=correct,heavy_check_mark=correct,thinking_face=partial,x=incorrect",
    ),
  DISPATCH_INTERVAL_MS: z.coerce.number().int().min(100).max(60_000).default(1_000),
  RCA_API_URL: z.string().url(),
  /** Answers are published here, which is not where questions arrive. */
  SLACK_ANSWER_CHANNEL_ID: z.string().min(1),
  /** The investigation's own ceiling. The workflow allowed 900 seconds. */
  RCA_TIMEOUT_MS: z.coerce.number().int().min(10_000).max(1_800_000).default(900_000),
  SLACK_POST_TIMEOUT_MS: z.coerce.number().int().min(1_000).max(60_000).default(30_000),
  /** Footnote destinations. Absent, a citation degrades to a bare id. */
  ZABBIX_FRONTEND_URL: z.string().optional(),
  KIBANA_URL: z.string().optional(),
  KIBANA_DATA_VIEW_ID: z.string().optional(),
  /** Mounted from the repository's templates/ directory. */
  TEMPLATE_DIR: z.string().default("/opt/aiops/templates"),
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
  dispatchIntervalMs: number;
  rcaApiUrl: string;
  slackAnswerChannelId: string;
  rcaTimeoutMs: number;
  slackPostTimeoutMs: number;
  zabbixFrontendUrl?: string;
  kibanaUrl?: string;
  kibanaDataViewId?: string;
  /**
   * Directory the report templates are read from at startup. The files decide
   * which reports exist; the table follows them.
   */
  templateDir: string;
  /** Emoji name to verdict. Reactions outside this map are ignored. */
  labelReactions: ReadonlyMap<string, FeedbackLabel>;
  /** Posts acknowledgements, reports, and requests for a written correction. */
  slackBotToken: string;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const parsed = envSchema.parse(env);
  const allowedUsers = new Set(
    (parsed.SLACK_ALLOWED_USER_IDS ?? "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean),
  );

  const config: AppConfig = {
    port: parsed.PORT,
    databaseUrl: parsed.DATABASE_URL,
    slackSigningSecret: parsed.SLACK_SIGNING_SECRET,
    slackQuestionChannelId: parsed.SLACK_QUESTION_CHANNEL_ID,
    ...(parsed.SLACK_BOT_USER_ID ? { slackBotUserId: parsed.SLACK_BOT_USER_ID } : {}),
    ...(parsed.SLACK_APP_ID ? { slackAppId: parsed.SLACK_APP_ID } : {}),
    slackAllowedUserIds: allowedUsers,
    internalToken: parsed.AIOPS_INTERNAL_TOKEN,
    dispatchIntervalMs: parsed.DISPATCH_INTERVAL_MS,
    templateDir: parsed.TEMPLATE_DIR,
    labelReactions: parseLabelReactions(parsed.SLACK_LABEL_REACTIONS),
    slackBotToken: parsed.SLACK_BOT_TOKEN,
    rcaApiUrl: parsed.RCA_API_URL,
    slackAnswerChannelId: parsed.SLACK_ANSWER_CHANNEL_ID,
    rcaTimeoutMs: parsed.RCA_TIMEOUT_MS,
    slackPostTimeoutMs: parsed.SLACK_POST_TIMEOUT_MS,
    ...(parsed.ZABBIX_FRONTEND_URL
      ? { zabbixFrontendUrl: parsed.ZABBIX_FRONTEND_URL }
      : {}),
    ...(parsed.KIBANA_URL ? { kibanaUrl: parsed.KIBANA_URL } : {}),
    ...(parsed.KIBANA_DATA_VIEW_ID
      ? { kibanaDataViewId: parsed.KIBANA_DATA_VIEW_ID }
      : {}),
  };

  return config;
}

/** Parses `emoji=verdict` pairs, failing loudly rather than silently dropping. */
function parseLabelReactions(value: string): ReadonlyMap<string, FeedbackLabel> {
  const map = new Map<string, FeedbackLabel>();
  for (const entry of value.split(",").map((part) => part.trim()).filter(Boolean)) {
    const [reaction, label] = entry.split("=").map((part) => part.trim());
    if (!reaction || !label) {
      throw new Error(`SLACK_LABEL_REACTIONS entry is not emoji=verdict: ${entry}`);
    }
    if (!FEEDBACK_LABELS.includes(label as FeedbackLabel)) {
      throw new Error(
        `SLACK_LABEL_REACTIONS verdict must be one of ${FEEDBACK_LABELS.join(", ")}: ${entry}`,
      );
    }
    map.set(reaction, label as FeedbackLabel);
  }
  return map;
}
