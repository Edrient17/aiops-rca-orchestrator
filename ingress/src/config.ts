import { z } from "zod";
import type { FeedbackLabel } from "./types.js";

const FEEDBACK_LABELS: readonly FeedbackLabel[] = ["correct", "partial", "incorrect"];

/**
 * rca-api's own ceiling on collecting evidence, restated here.
 *
 * It is `InvestigationLimits.max_duration_seconds`, which the graph's stop
 * guard enforces and `prepare_collection` defaults to 600 seconds. This
 * service cannot read it -- it belongs to the other process -- so it is named
 * rather than inferred, and the floor it sets on RCA_TIMEOUT_MS is what keeps
 * this service from hanging up on a run the far side is still working on.
 *
 * If that default changes, this changes with it. A copy that drifts fails in
 * the safe direction -- a timeout larger than it needs to be -- but it is a
 * copy, and worth saying so.
 */
const RCA_COLLECTION_CEILING_MS = 600_000;

/** What writing the report costs after collection stops. Measured at 21s. */
const RCA_WRITING_HEADROOM_MS = 60_000;

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
  /**
   * How long one investigation may take. This is the outermost of three
   * ceilings and has to stay the largest of them, or it stops being a limit
   * and becomes a way of throwing away work that was going to finish.
   *
   *   rca-api's collection loop   600s   graph max_duration_seconds
   *   + writing the report          ~30s   measured at 21s
   *   ------------------------------------
   *   RCA_TIMEOUT_MS              900s   this, and now also the transport's
   *
   * The order was inverted and invisible: the transport gave up at 300
   * seconds of its own accord, under a value that said 900, so the smallest
   * ceiling was the one nobody had written down. The floor below is what
   * stops it being inverted again by configuration -- setting this under the
   * collection loop's own ceiling would cut off investigations that rca-api
   * was still willing to finish.
   */
  RCA_TIMEOUT_MS: z.coerce
    .number()
    .int()
    .min(RCA_COLLECTION_CEILING_MS + RCA_WRITING_HEADROOM_MS)
    .max(1_800_000)
    .default(900_000),
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
