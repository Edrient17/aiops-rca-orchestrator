/**
 * What the status stored against a request says about the asker.
 *
 * The column is free text -- aiops_requests has no CHECK constraint on it --
 * and four places write it: this service's pipeline, saveReport when an answer
 * is published, saveSlackRequest when a continuation arrives, and the
 * dispatcher when it gives up. Reading one of those values back as a decision
 * means the writers and the reader agreeing on what it means, and that
 * agreement is what lives here rather than in whichever file happened to need
 * it first.
 */

/**
 * The statuses that mean this request's clarification has already been asked.
 *
 * `needs_clarification` and `unsupported` are written by runInvestigation
 * itself, from the status the RCA service returned: it answers with exactly
 * one of "completed", "needs_clarification" and "unsupported", and the first
 * is the only one that produces a report. `clarified` is written elsewhere --
 * saveSlackRequest sets it on the parent when the asker's reply lands -- and
 * belongs here because a question that has been answered was asked. Putting it
 * back in front of someone who has already replied to it is the same bug, one
 * step worse.
 *
 * What is deliberately absent carries as much weight as what is here.
 * `analyzing_question` is written by every attempt on its way past, so it says
 * nothing about what any of them reached. `failed` means a delivery was given
 * up on, which is not a question anybody was sent. `completed` is the report
 * path's outcome and not this list's business.
 */
export const CLARIFICATION_ASKED: readonly string[] = [
  "needs_clarification",
  "unsupported",
  "clarified",
];

/**
 * Every status that means the request already reached an outcome the asker saw.
 *
 * These are the statuses a later bookkeeping failure must not relabel. The rule
 * predates this list in the narrow form `status <> 'completed'`, written so
 * that a dispatcher recording its own failure to close a row could not retract
 * a report that had already been published. A clarification is published the
 * same way and needs the same protection -- more so, because the stored status
 * is the only record that it was asked at all, where a report also has its own
 * row.
 */
export const SETTLED_STATUSES: readonly string[] = ["completed", ...CLARIFICATION_ASKED];
