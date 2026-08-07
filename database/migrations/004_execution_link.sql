-- Tie an n8n execution back to the request it is running.
--
-- When a run fails, the error workflow is handed the execution id but not the
-- request id: n8n does not populate error.context, so the only correlation key
-- it can offer is its own execution. Without a mapping the failure is recorded
-- with request_id NULL, the request is never moved off its in-progress status,
-- and it sits in analyzing_question forever while the Slack error channel
-- reports a failure nobody can attribute.
--
-- The main workflow registers this id before it does anything that can fail, so
-- the mapping already exists by the time the error handler needs it.
ALTER TABLE aiops_requests
  ADD COLUMN IF NOT EXISTS n8n_execution_id text;

-- Not unique: a manual re-run in the n8n UI executes the same request again
-- under a new id, and the newest execution is the one an error refers to.
CREATE INDEX IF NOT EXISTS aiops_requests_execution_idx
  ON aiops_requests (n8n_execution_id)
  WHERE n8n_execution_id IS NOT NULL;
