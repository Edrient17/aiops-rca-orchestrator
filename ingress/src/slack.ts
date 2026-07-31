import { createHmac, timingSafeEqual } from "node:crypto";
import type { SlackEventEnvelope } from "./types.js";

const MAX_TIMESTAMP_SKEW_SECONDS = 5 * 60;

export function createSlackSignature(
  signingSecret: string,
  timestamp: string,
  rawBody: Buffer | string,
): string {
  return `v0=${createHmac("sha256", signingSecret)
    .update(`v0:${timestamp}:${rawBody.toString()}`)
    .digest("hex")}`;
}

export function verifySlackSignature(input: {
  signingSecret: string;
  timestamp: string | undefined;
  signature: string | undefined;
  rawBody: Buffer;
  nowSeconds?: number;
}): boolean {
  if (!input.timestamp || !input.signature || !/^\d+$/.test(input.timestamp)) {
    return false;
  }

  const nowSeconds = input.nowSeconds ?? Math.floor(Date.now() / 1_000);
  if (Math.abs(nowSeconds - Number(input.timestamp)) > MAX_TIMESTAMP_SKEW_SECONDS) {
    return false;
  }

  const expected = Buffer.from(
    createSlackSignature(input.signingSecret, input.timestamp, input.rawBody),
    "utf8",
  );
  const actual = Buffer.from(input.signature, "utf8");

  return expected.length === actual.length && timingSafeEqual(expected, actual);
}

export function isSlackEventEnvelope(value: unknown): value is SlackEventEnvelope {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const envelope = value as Record<string, unknown>;
  if (
    envelope.type !== "event_callback" ||
    typeof envelope.event_id !== "string" ||
    typeof envelope.event !== "object" ||
    envelope.event === null
  ) {
    return false;
  }

  return typeof (envelope.event as Record<string, unknown>).type === "string";
}

export function stripBotMention(text: string, botUserId?: string): string {
  if (!botUserId) {
    return text.trim();
  }

  return text.replace(new RegExp(`<@${escapeRegExp(botUserId)}>`, "g"), "").trim();
}

export function makeRequestId(eventId: string, epochSeconds?: number): string {
  const date = new Date((epochSeconds ?? Math.floor(Date.now() / 1_000)) * 1_000);
  const datePart = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  })
    .format(date)
    .replaceAll("-", "");
  const safeEventId = eventId.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 120);
  return `REQ-${datePart}-${safeEventId}`;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
