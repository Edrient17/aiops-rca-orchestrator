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
   * Where a claimed request is delivered. Named rather than inlined so the
   * queue, the retry and the abandonment can be tested without an
   * investigation, and so this file has no opinion about what running one
   * involves.
   */
  deliver: Deliver;
  /**
   * Told when a request is abandoned. This is the only path by which the asker
   * learns their question died -- see `abandon` -- so leaving it out is
   * choosing silence.
   */
  announce?: Announce;
}

export type Deliver = (job: DispatchJob) => Promise<void>;

/**
 * How a request that has been given up on is announced to whoever asked it.
 *
 * Injected for the reason `deliver` is: the queue decides when a question is
 * dead, it does not decide how someone is told. Optional, because a dispatcher
 * under test has nobody to tell.
 */
export type Announce = (job: DispatchJob, reason: string) => Promise<void>;



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
 * Nine leaves a window of about eight and a half minutes -- the backoff below,
 * summed: 2 + 4 + 8 + 16 + 32 + 64 + 128 + 256 seconds. That is measured rather
 * than argued, and the twelve this used to be was measured the same way: it
 * came to seventeen minutes, not the hour the comment claimed.
 *
 * The number counts claims, and claimDispatch increments before it returns, so
 * the check below compares it directly. It read `attempts + 1 >=` while this
 * said twelve, which stopped after eleven deliveries and then recorded twelve
 * -- wrong in both directions at once, in a count nobody could check without
 * the queue in front of them.
 *
 * What the window has to outlast is a deploy. ingress does not wait for rca-api
 * -- there is no depends_on between them -- so it comes back dispatching while
 * rca-api is still starting, and every claim in that gap fails at once. What it
 * does not have to outlast is a dependency that has genuinely gone.
 *
 * It is set from the low side because the error is not symmetric. Giving up too
 * late costs the asker a wait. Giving up too early costs them the question:
 * completeDispatch stamps dispatched_at, claimDispatch takes only rows where it
 * is null, and so an abandonment is final. There is no later attempt to save a
 * question this was wrong about.
 */
export const MAX_DISPATCH_ATTEMPTS = 9;

/**
 * How long a claimed job stays locked. Derived from the delivery timeout rather
 * than fixed, and the derivation has now earned itself twice. It was written
 * because a 30-second hardcoded lock could lapse while a slow webhook was still
 * receiving the request. It mattered again when delivery became the whole
 * investigation: a lock sized for a POST would expire minutes into a run, and a
 * second dispatcher would start the same investigation over.
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
          if (job.attempts >= MAX_DISPATCH_ATTEMPTS) {
            await this.abandon(job, message);
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
   * row nobody queries. Then the request stops claiming to be in progress, and
   * the failure is written down for whoever operates this.
   *
   * The announcement is last, and is the only one of the four the asker ever
   * sees. It used to be missing: the error was written to aiops_system_errors
   * and that was counted as telling them, because n8n's error workflow read
   * that table and posted what it found. Removing n8n removed the only reader.
   * Nothing caught it, because what stayed behind still looked like a
   * notification. A question acknowledged and then silent is the failure this
   * queue exists to prevent, so it is sent now rather than filed.
   */
  private async abandon(job: DispatchJob, message: string): Promise<void> {
    // job.attempts, not the ceiling: they are equal when the ceiling is what
    // stopped this, and only the first is true if it is ever lowered under a
    // queue that already holds rows counted against the old one.
    const detail = `the investigation failed ${job.attempts} times: ${message}`;
    await this.options.repository.completeDispatch(job.id);
    await this.options.repository.updateRequestStatus(job.requestId, "failed", detail);
    await this.options.repository.recordSystemError({
      requestId: job.requestId,
      workflowName: "ingress dispatcher",
      message: detail,
    });

    // Swallowed on purpose. This runs inside the catch around a delivery, so a
    // throw here leaves the claim loop and takes the dispatcher down over a
    // Slack outage. The queue row is already closed above, so failing to
    // announce cannot revive the job -- it can only go unheard, which is what
    // the second record is for.
    try {
      await this.options.announce?.(job, message);
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      await this.options.repository.recordSystemError({
        requestId: job.requestId,
        workflowName: "ingress dispatcher",
        message: `abandonment could not be announced: ${reason}`,
      });
    }
  }
}
