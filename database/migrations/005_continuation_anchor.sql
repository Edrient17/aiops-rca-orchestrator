-- Repoint continuation requests at the thread they actually live in.
--
-- Mark Analyzing used to take the anchor from `thread_ts` on the
-- chat.postMessage response -- a field Slack returns nested inside `message`,
-- not at the top level. The expression therefore always fell through to its
-- fallback, and every request that answered a clarification recorded its own
-- reply ts as the thread root instead of the root it was posted into.
--
-- Slack hid this. A thread_ts pointing at a reply resolves to that reply's
-- parent, so the reports still landed in the right thread and nothing looked
-- wrong in the channel. Only the stored value was wrong, and only one thing
-- reads it: findReportByThread, which matches it against the root ts that a
-- threaded reply carries. It missed, so a correction written under one of these
-- reports was ignored rather than kept in aiops_report_notes -- silently losing
-- exactly the ground truth the feedback loop exists to collect.
--
-- The right anchor is the root of the clarification chain: a continuation was
-- posted into its parent's thread, and the first request in that chain started
-- the thread. Resolved recursively rather than one level up, so a question that
-- took several rounds of clarification is repaired in a single pass.
--
-- Idempotent, as every file here must be: afterwards each child already equals
-- its root, so the IS DISTINCT FROM guard makes a second run match nothing.
--
-- A request with no parent is never touched. Usually that is a genuine root,
-- whose anchor was correct all along. It can also be a continuation whose
-- parent row is gone, and the two are indistinguishable here -- once
-- parent_request_id is NULL nothing marks which it was. Such a row cannot be
-- repaired in any case: the anchor it needs lived on the parent and nowhere
-- else, not in aiops_reports, and not in raw_payload, whose thread_ts refers to
-- the question channel rather than the answer thread this column names. 003
-- was the one thing that could manufacture that state, by deleting a duplicate
-- a continuation pointed at; it now reassigns the child to the surviving twin
-- first, so this file sees a parent wherever one ever existed.
WITH RECURSIVE chain AS (
  SELECT request_id, request_id AS root_id
  FROM aiops_requests
  WHERE parent_request_id IS NULL

  UNION ALL

  SELECT child.request_id, chain.root_id
  FROM aiops_requests AS child
  JOIN chain ON child.parent_request_id = chain.request_id
)
UPDATE aiops_requests AS target
SET slack_ack_ts = root.slack_ack_ts,
    updated_at = now()
FROM chain
JOIN aiops_requests AS root ON root.request_id = chain.root_id
WHERE target.request_id = chain.request_id
  AND chain.request_id <> chain.root_id
  AND root.slack_ack_ts IS NOT NULL
  AND target.slack_ack_ts IS DISTINCT FROM root.slack_ack_ts;
