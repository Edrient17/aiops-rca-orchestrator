import type { DispatchJob, RequestRepository } from "./types.js";

export interface DispatcherOptions {
  repository: RequestRepository;
  internalToken: string;
  intervalMs: number;
  /**
   * How long one delivery may take. Also what the claim lock is derived from,
   * so the two cannot drift apart -- see lockSecondsFor.
   */
  timeoutMs: number;
  /**
   * Where a claimed request is delivered. Handing this in rather than
   * hardcoding the n8n POST is what lets the same queue, the same retry and the
   * same abandonment run an investigation in this process instead.
   */
  deliver: Deliver;
  /** Named in the message when delivery is given up on. */
  targetName?: string;
}

export type Deliver = (job: DispatchJob) => Promise<void>;

/**
 * Delivery by POST to an n8n webhook, which is what this queue did for its
 * whole life before the workflow moved in here.
 */
export function deliverToWebhook(options: {
  webhookUrl: string;
  internalToken: string;
  timeoutMs: number;
  fetchImpl?: typeof fetch;
}): Deliver {
  const send = options.fetchImpl ?? fetch;
  return async (job) => {
    const response = await send(options.webhookUrl, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-aiops-internal-token": options.internalToken,
      },
      body: JSON.stringify(job.payload),
      signal: AbortSignal.timeout(options.timeoutMs),
    });
    if (!response.ok) {
      throw new Error(`n8n webhook returned HTTP ${response.status}`);
    }
  };
}

/**
 * Slack left between a claim expiring and the delivery it belongs to finishing.
 * Covers the database round trip that records the outcome, so the lock is still
 * held when the job is marked done or rescheduled.
 */
const LOCK_MARGIN_SECONDS = 15;

/**
 * How many deliveries a request gets before it is given up on.
 *
 * The retry was unbounded, which sounds harmless -- the backoff tops out at
 * five minutes and the row is small. The harm is to the asker: a question whose
 * delivery can never succeed sat in the queue forever with an acknowledgement
 * already posted and no answer ever coming, and nothing anywhere said so.
 *
 * Twelve attempts with the backoff below is a little over an hour, which
 * outlasts an n8n restart or a deploy by a wide margin. What it does not
 * outlast is a webhook that has genuinely gone.
 */
const MAX_DISPATCH_ATTEMPTS = 12;

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

export class Dispatcher {
  private timer: NodeJS.Timeout | undefined;
  private running = false;
  private readonly lockSeconds: number;

  constructor(private readonly options: DispatcherOptions) {
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
          await this.options.deliver(job);
          await this.options.repository.completeDispatch(job.id);
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          if (job.attempts + 1 >= MAX_DISPATCH_ATTEMPTS) {
            await this.abandon(job.id, job.requestId, message);
            continue;
          }
          const delaySeconds = Math.min(300, 2 ** Math.min(job.attempts, 8));
          await this.options.repository.retryDispatch(job.id, delaySeconds, message);
        }
      }
    } finally {
      this.running = false;
    }
  }

  /**
   * Stop trying, and make the failure visible.
   *
   * completeDispatch is what takes the job out of the due set -- there is no
   * separate abandoned state, and inventing one would mean a migration for a
   * row nobody queries. What matters is the other two writes: the request stops
   * claiming to be in progress, and the error lands where the error channel
   * reads from, so the asker learns their question died instead of waiting on
   * an answer that was never coming.
   */
  private async abandon(jobId: number, requestId: string, message: string): Promise<void> {
    const target = this.options.targetName ?? "the investigation pipeline";
    const detail = `delivery to ${target} failed ${MAX_DISPATCH_ATTEMPTS} times: ${message}`;
    await this.options.repository.completeDispatch(jobId);
    await this.options.repository.updateRequestStatus(requestId, "failed", detail);
    await this.options.repository.recordSystemError({
      requestId,
      workflowName: "ingress dispatcher",
      message: detail,
    });
  }
}
