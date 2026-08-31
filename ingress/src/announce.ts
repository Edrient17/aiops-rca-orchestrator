import type { Announce } from "./dispatcher.js";
import { abandonedText, type DispatchPayload } from "./pipeline.js";
import { postMessage } from "./slack.js";

/** Only what the announcement needs; the loaded config satisfies it. */
export interface AnnounceConfig {
  slackBotToken: string;
  slackAnswerChannelId: string;
  slackPostTimeoutMs: number;
}

/**
 * Where a question the queue gave up on is reported.
 *
 * Under the acknowledgement when there is one, because that thread is the
 * investigation and it is the one left waiting. Without an anchor the request
 * died before it was ever acknowledged, so there is no thread of ours to speak
 * into and the question channel is where the asker is.
 *
 * A factory rather than an inline callback in server.ts, which cannot be
 * imported by a test: it opens a pool and starts listening at module scope. The
 * choice of channel and the choice of thread have to agree with each other, and
 * that agreement is exactly what was wrong here before -- so it is somewhere a
 * test can reach.
 */
export function createAbandonmentAnnouncer(
  config: AnnounceConfig,
  post: typeof postMessage = postMessage,
): Announce {
  return async (job, reason) => {
    const payload = job.payload as DispatchPayload;
    // `|| null` rather than reading the field twice: the channel is chosen on
    // truthiness and the thread on nullishness, and an empty string would
    // otherwise pick the question channel while still being passed as the
    // thread to reply in -- which postMessage then drops, stranding the notice
    // at the top of the channel. One reading of "no anchor" for both.
    const anchor = payload.slack_ack_ts || null;

    await post({
      botToken: config.slackBotToken,
      channel: anchor ? config.slackAnswerChannelId : payload.channel_id,
      text: abandonedText(payload, reason),
      threadTs: anchor ?? payload.thread_ts ?? payload.message_ts,
      timeoutMs: config.slackPostTimeoutMs,
    });
  };
}
