import { Pool } from "pg";
import { createApp } from "./app.js";
import { loadConfig } from "./config.js";
import { N8nDispatcher } from "./dispatcher.js";
import { PostgresRequestRepository } from "./postgres-repository.js";

const config = loadConfig();
const pool = new Pool({
  connectionString: config.databaseUrl,
  max: 10,
  connectionTimeoutMillis: 5_000,
  idleTimeoutMillis: 30_000,
});
const repository = new PostgresRequestRepository(pool);
const dispatcher = new N8nDispatcher({
  repository,
  webhookUrl: config.n8nWebhookUrl,
  internalToken: config.internalToken,
  intervalMs: config.dispatchIntervalMs,
  timeoutMs: config.dispatchTimeoutMs,
});
const app = createApp({
  config,
  repository,
  onRequestAccepted: () => dispatcher.wake(),
});
const server = app.listen(config.port, "0.0.0.0", () => {
  console.log(
    JSON.stringify({
      level: "info",
      message: "Slack ingress listening",
      port: config.port,
    }),
  );
  dispatcher.start();
});

async function shutdown(signal: string): Promise<void> {
  console.log(JSON.stringify({ level: "info", message: "Shutting down", signal }));
  dispatcher.stop();
  server.close(async () => {
    await pool.end();
    process.exit(0);
  });
  setTimeout(() => process.exit(1), 10_000).unref();
}

process.on("SIGTERM", () => void shutdown("SIGTERM"));
process.on("SIGINT", () => void shutdown("SIGINT"));
