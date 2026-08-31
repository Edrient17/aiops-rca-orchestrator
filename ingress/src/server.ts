import { Pool } from "pg";
import { createAbandonmentAnnouncer } from "./announce.js";
import { createApp } from "./app.js";
import { loadConfig } from "./config.js";
import { Dispatcher } from "./dispatcher.js";
import { runInvestigation, type DispatchPayload } from "./pipeline.js";
import { PostgresRequestRepository } from "./postgres-repository.js";
import { syncTemplates } from "./template-sync.js";

const config = loadConfig();
const pool = new Pool({
  connectionString: config.databaseUrl,
  max: 10,
  connectionTimeoutMillis: 5_000,
  idleTimeoutMillis: 30_000,
});
const repository = new PostgresRequestRepository(pool);
/**
 * One delivery is one whole investigation, and the claim lock is derived from
 * how long that may take. The Slack posts on either side of it are counted in
 * because they happen while the job is still held.
 */
const dispatcher = new Dispatcher({
  repository,
  internalToken: config.internalToken,
  intervalMs: config.dispatchIntervalMs,
  timeoutMs: config.rcaTimeoutMs + 2 * config.slackPostTimeoutMs,
  deliver: (job) =>
    runInvestigation(job.payload as DispatchPayload, { repository }, {
      answerChannelId: config.slackAnswerChannelId,
      rcaApiUrl: config.rcaApiUrl,
      internalToken: config.internalToken,
      botToken: config.slackBotToken,
      rcaTimeoutMs: config.rcaTimeoutMs,
      slackTimeoutMs: config.slackPostTimeoutMs,
      format: {
        zabbixFrontendUrl: config.zabbixFrontendUrl,
        kibanaUrl: config.kibanaUrl,
        kibanaDataViewId: config.kibanaDataViewId,
      },
    }),
  announce: createAbandonmentAnnouncer(config),
});
const app = createApp({
  config,
  repository,
  onRequestAccepted: () => dispatcher.wake(),
});

/**
 * Bring the registry in line with the files before serving anything.
 *
 * A malformed template only shows itself midway through an investigation, once
 * a question has already been accepted and acknowledged, so it is refused here
 * instead -- while the deploy is still watching. Failing to start holds the old
 * container in place, which is the same bargain db-migrate makes.
 */
async function start(): Promise<void> {
  const result = await syncTemplates({
    repository,
    directory: config.templateDir,
    log: (message, fields) =>
      console.log(JSON.stringify({ level: "warn", message, ...fields })),
  });
  console.log(
    JSON.stringify({
      level: "info",
      message: "Report templates synced",
      created: result.created,
      updated: result.updated,
      unchanged: result.unchanged.length,
      removed: result.removed,
      ...(result.skippedRemoval ? { removal_skipped: true } : {}),
    }),
  );

  server = app.listen(config.port, "0.0.0.0", () => {
    console.log(
      JSON.stringify({
        level: "info",
        message: "Slack ingress listening",
        port: config.port,
      }),
    );
    dispatcher.start();
  });
}

let server: ReturnType<typeof app.listen> | undefined;

void start().catch((error: unknown) => {
  console.error(
    JSON.stringify({
      level: "error",
      message: "Startup failed",
      detail: error instanceof Error ? error.message : String(error),
    }),
  );
  process.exit(1);
});

async function shutdown(signal: string): Promise<void> {
  console.log(JSON.stringify({ level: "info", message: "Shutting down", signal }));
  dispatcher.stop();
  if (!server) {
    await pool.end();
    process.exit(0);
  }
  server.close(async () => {
    await pool.end();
    process.exit(0);
  });
  setTimeout(() => process.exit(1), 10_000).unref();
}

process.on("SIGTERM", () => void shutdown("SIGTERM"));
process.on("SIGINT", () => void shutdown("SIGINT"));
