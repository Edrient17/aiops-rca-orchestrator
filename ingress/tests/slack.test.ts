import { describe, expect, it } from "vitest";
import {
  createSlackSignature,
  makeRequestId,
  stripBotMention,
  verifySlackSignature,
} from "../src/slack.js";

describe("Slack signature verification", () => {
  it("accepts a valid recent signature", () => {
    const timestamp = "1700000000";
    const rawBody = Buffer.from('{"type":"event_callback"}');
    const signature = createSlackSignature("secret", timestamp, rawBody);

    expect(
      verifySlackSignature({
        signingSecret: "secret",
        timestamp,
        signature,
        rawBody,
        nowSeconds: 1700000001,
      }),
    ).toBe(true);
  });

  it("rejects stale timestamps and modified bodies", () => {
    const timestamp = "1700000000";
    const signature = createSlackSignature("secret", timestamp, "original");

    expect(
      verifySlackSignature({
        signingSecret: "secret",
        timestamp,
        signature,
        rawBody: Buffer.from("modified"),
        nowSeconds: 1700000001,
      }),
    ).toBe(false);
    expect(
      verifySlackSignature({
        signingSecret: "secret",
        timestamp,
        signature,
        rawBody: Buffer.from("original"),
        nowSeconds: 1700001000,
      }),
    ).toBe(false);
  });
});

describe("Slack request normalization", () => {
  it("removes the bot mention and builds a stable KST request id", () => {
    expect(stripBotMention("<@U-BOT> web-01 CPU 장애 조사", "U-BOT")).toBe(
      "web-01 CPU 장애 조사",
    );
    expect(makeRequestId("Ev-123", 1_700_000_000)).toBe("REQ-20231115-Ev-123");
  });
});
