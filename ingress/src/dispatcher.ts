import type { RequestRepository } from "./types.js";

export interface DispatcherOptions {
  repository: RequestRepository;
  webhookUrl: string;
  internalToken: string;
  intervalMs: number;
  timeoutMs: number;
  fetchImpl?: typeof fetch;
}

/**
 * Slack left between a claim expiring and the delivery it belongs to finishing.
 * Covers the database round trip that records the outcome, so the lock is still
 * held when the job is marked done or rescheduled.
 */
const LOCK_MARGIN_SECONDS = 15;

/**
 * How long a claimed job stays locked. Derived from the delivery timeout rather
 * than fixed: the lock was hardcoded at 30 seconds while DISPATCH_TIMEOUT_MS
 * accepts up to 60_000, so a slow n8n could still be receiving a request whose
 * claim had already lapsed, letting another dispatcher pick it up and run the
 * same investigation twice.
 */
export function lockSecondsFor(timeoutMs: number): number {
  return Math.ceil(timeoutMs / 1_000) + LOCK_MARGIN_SECONDS;
}

export class N8nDispatcher {
  private timer: NodeJS.Timeout | undefined;
  private running = false;
  private readonly fetchImpl: typeof fetch;
  private readonly lockSeconds: number;

  constructor(private readonly options: DispatcherOptions) {
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.lockSeconds = lockSecondsFor(options.timeoutMs);
  }

  start(): void {
    if (this.timer) {
      return;
    }
    this.timer = setInterval(() => void this.runOnce(), this.options.intervalMs);
    void this.runOnce();
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
  }

  wake(): void {
    void this.runOnce();
  }

  async runOnce(): Promise<void> {
    if (this.running) {
      return;
    }
    this.running = true;
    try {
      while (true) {
        const job = await this.options.repository.claimDispatch(this.lockSeconds);
        if (!job) {
          return;
        }

        try {
          const response = await this.fetchImpl(this.options.webhookUrl, {
            method: "POST",
            headers: {
              "content-type": "application/json",
              "x-aiops-internal-token": this.options.internalToken,
            },
            body: JSON.stringify(job.payload),
            signal: AbortSignal.timeout(this.options.timeoutMs),
          });

          if (!response.ok) {
            throw new Error(`n8n webhook returned HTTP ${response.status}`);
          }

          await this.options.repository.completeDispatch(job.id);
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          const delaySeconds = Math.min(300, 2 ** Math.min(job.attempts, 8));
          await this.options.repository.retryDispatch(job.id, delaySeconds, message);
        }
      }
    } finally {
      this.running = false;
    }
  }
}
