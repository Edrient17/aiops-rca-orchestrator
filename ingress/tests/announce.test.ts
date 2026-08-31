/**
 * Where an abandonment is posted, and what it is threaded under.
 *
 * This lived as an inline callback in server.ts, which no test can import --
 * it opens a pool and starts listening at module scope -- so the two halves of
 * the decision, the channel and the thread, agreed with each other only by
 * inspection. They did not: the notice was going to the answer channel while
 * telling the asker to do something only the question channel accepts.
 */

import { describe, expect, it } from "vitest";
import { createAbandonmentAnnouncer } from "../src/announce.js";
import type { DispatchJob } from "../src/types.js";

const CONFIG = {
  slackBotToken: "xoxb-test",
  slackAnswerChannelId: "C-ANSWER",
  slackPostTimeoutMs: 5_000,
};

function jobWith(payload: Partial<DispatchJob["payload"]>): DispatchJob {
  return {
    id: 1,
    requestId: "REQ-1",
    attempts: 9,
    payload: {
      request_id: "REQ-1",
      slack_event_id: "EV1",
      team_id: "T1",
      channel_id: "C-QUESTION",
      user_id: "U-ASKER",
      message_ts: "1000.1",
      thread_ts: null,
      question: "장애 조사",
      received_at: "2026-08-20T00:00:00Z",
      parent_request_id: null,
      prior_question: null,
      parent_ack_ts: null,
      slack_ack_ts: null,
      ...payload,
    },
  };
}

function recordingPost() {
  const posts: Record<string, unknown>[] = [];
  const post = async (input: Record<string, unknown>) => {
    posts.push(input);
    return { ts: "9999.1" };
  };
  return { posts, post: post as never };
}

describe("announcing an abandonment", () => {
  it("replies under the acknowledgement when the question was acknowledged", async () => {
    const { posts, post } = recordingPost();
    const announce = createAbandonmentAnnouncer(CONFIG, post);

    await announce(jobWith({ slack_ack_ts: "1234.5" }), "RCA service returned HTTP 500");

    expect(posts).toHaveLength(1);
    expect(posts[0]).toMatchObject({ channel: "C-ANSWER", threadTs: "1234.5" });
  });

  it("falls back to the question channel when it was never acknowledged", async () => {
    // No anchor means the request died before a thread of ours existed, so the
    // only place the asker is waiting is where they asked.
    const { posts, post } = recordingPost();
    const announce = createAbandonmentAnnouncer(CONFIG, post);

    await announce(jobWith({ slack_ack_ts: null }), "boom");

    expect(posts[0]).toMatchObject({ channel: "C-QUESTION", threadTs: "1000.1" });
  });

  it("joins the question's own thread when it had one", async () => {
    const { posts, post } = recordingPost();
    const announce = createAbandonmentAnnouncer(CONFIG, post);

    await announce(jobWith({ slack_ack_ts: null, thread_ts: "500.1" }), "boom");

    expect(posts[0]).toMatchObject({ channel: "C-QUESTION", threadTs: "500.1" });
  });

  it("reads an empty anchor the same way in both halves of the decision", async () => {
    // The channel was chosen on truthiness and the thread on nullishness, so
    // "" picked the question channel and then passed "" as the thread -- which
    // postMessage drops, stranding the notice at the top of the channel.
    const { posts, post } = recordingPost();
    const announce = createAbandonmentAnnouncer(CONFIG, post);

    await announce(jobWith({ slack_ack_ts: "" }), "boom");

    expect(posts[0]).toMatchObject({ channel: "C-QUESTION", threadTs: "1000.1" });
  });

  it("carries the reason into the message", async () => {
    const { posts, post } = recordingPost();
    const announce = createAbandonmentAnnouncer(CONFIG, post);

    await announce(jobWith({}), "RCA service returned HTTP 500");

    expect(posts[0]?.text).toContain("RCA service returned HTTP 500");
  });
});
