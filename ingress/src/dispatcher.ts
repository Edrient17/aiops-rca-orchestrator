import type { RequestRepository } from "./types.js";

export interface DispatcherOptions {
  repository: RequestRepository;
  webhookUrl: string;
  internalToken: string;
  intervalMs: number;
  timeoutMs: number;
  fetchImpl?: typeof fetch;
}

export class N8nDispatcher {
  private timer: NodeJS.Timeout | undefined;
  private running = false;
  private readonly fetchImpl: typeof fetch;

  constructor(private readonly options: DispatcherOptions) {
    this.fetchImpl = options.fetchImpl ?? fetch;
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
        const job = await this.options.repository.claimDispatch();
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
