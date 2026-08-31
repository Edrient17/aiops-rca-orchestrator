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
   * learns their question died -- see `abandon` -- so it is required rather
   * than optional. Omitting it is choosing silence, and silence is the exact
   * failure this queue grew the announcement to fix: a wiring that drops it
   * should fail to compile, because no test can see it go missing.
   */
  announce: Announce;
}

export type Deliver = (job: DispatchJob) => Promise<void>;

/**
 * How a request that has been given up on is announced to whoever asked it.
 *
 * Injected for the reason `deliver` is: the queue decides when a question is
 * dead, it does not decide how someone is told. A dispatcher under test passes
 * one that records the call rather than posting it.
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
    this.timer = setInterval(() => this.sweep(), this.options.intervalMs);
    this.sweep();
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
  }

  wake(): void {
    this.sweep();
  }

  /**
   * Start a pass, and absorb anything that escapes it.
   *
   * runOnce is fired and forgotten from three places, and Node ends the
   * process on an unhandled rejection. Without this, a database that is
   * unreachable when a job is claimed takes ingress down with it -- and
   * `restart: unless-stopped` brings it straight back to the same job and the
   * same throw. A pass that fails is a pass skipped; the next tick reclaims
   * whatever it left, because the lock lapses on its own.
   */
  private sweep(): void {
    void this.runOnce().catch((error: unknown) => {
      console.error(
        JSON.stringify({
          level: "error",
          message: "dispatch_pass_failed",
          detail: error instanceof Error ? error.message : String(error),
        }),
      );
    });
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
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          if (job.attempts >= MAX_DISPATCH_ATTEMPTS) {
            await this.abandon(job, message);
            continue;
          }
          // 2 to 256 seconds. attempts is 1 on the first claim and 9 abandons,
          // so this only ever sees 1..8 -- the clamps this used to carry were
          // sized for a ceiling of twelve and could no longer bind.
          await this.options.repository.retryDispatch(job.id, 2 ** job.attempts, message);
          continue;
        }

        // Only reached once the investigation itself succeeded, which is why
        // it is not inside the try above. A failure to close the row is
        // bookkeeping: sending it back through that catch would retry a
        // delivery whose report is already posted, and on the last claim it
        // would abandon the job and tell the asker that an answered question
        // failed. The row is left for the lock to lapse instead.
        await this.runQuietly(job.requestId, "the delivered job could not be closed", () =>
          this.options.repository.completeDispatch(job.id),
        );
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
   *
   * Being last is why the three writes before it are guarded individually
   * rather than run as one sequence. They are independent, and there is no
   * later attempt to save a question -- completeDispatch has already taken the
   * row out of the due set. A status update that failed used to throw straight
   * past the announcement, leaving the request reading as in progress forever
   * and the asker hearing nothing: the failure being repaired here, reached by
   * a different route.
   */
  private async abandon(job: DispatchJob, message: string): Promise<void> {
    // job.attempts, not the ceiling: they are equal when the ceiling is what
    // stopped this, and only the first is true if it is ever lowered under a
    // queue that already holds rows counted against the old one.
    const detail = `the investigation failed ${job.attempts} times: ${message}`;

    // Not guarded, and first: until this lands the job is still live, and a
    // throw here should leave it that way to be claimed again rather than
    // announce a death the queue has not recorded. sweep() catches it.
    await this.options.repository.completeDispatch(job.id);

    await this.runQuietly(job.requestId, "the request could not be marked failed", () =>
      this.options.repository.updateRequestStatus(job.requestId, "failed", detail),
    );
    await this.runQuietly(job.requestId, "the abandonment could not be recorded", () =>
      this.options.repository.recordSystemError({
        requestId: job.requestId,
        workflowName: "ingress dispatcher",
        message: detail,
      }),
    );
    await this.runQuietly(job.requestId, "abandonment could not be announced", () =>
      this.options.announce(job, message),
    );
  }

  /**
   * Run one closing step, and keep going if it fails.
   *
   * All of this runs inside the catch around a delivery, where a throw leaves
   * the claim loop -- so Slack being down, or the database being down, would
   * otherwise stop the dispatcher outright. The note about the failure is
   * itself guarded: when the database is the thing that is down, recording
   * that fact fails too, and a recovery path that can throw is not one.
   */
  private async runQuietly(
    requestId: string,
    what: string,
    step: () => Promise<unknown>,
  ): Promise<void> {
    try {
      await step();
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      try {
        await this.options.repository.recordSystemError({
          requestId,
          workflowName: "ingress dispatcher",
          message: `${what}: ${reason}`,
        });
      } catch {
        // Nothing left that can be written to. Losing the note is not a
        // reason to end the claim loop.
      }
    }
  }
}
