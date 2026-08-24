-- Drop the execution link. Nothing writes it any more.

-- 004 added aiops_requests.n8n_execution_id so a failure could be attributed.
-- n8n handed its error workflow an execution id and not a request id -- it does
-- not populate error.context -- so the main workflow registered the mapping
-- before doing anything that could fail, and the error handler looked the
-- request up through it. Removing n8n removed the only writer: the dispatcher
-- that replaced it fails inside the process holding the request and passes the
-- request id directly, so the column has been read-only and empty since.
--
-- 004 is gone rather than superseded. Every file here re-applies on every
-- start, so leaving it would add this column and drop it again on each deploy,
-- forever. The reason it existed is written down above instead.
DROP INDEX IF EXISTS aiops_requests_execution_idx;

ALTER TABLE aiops_requests
  DROP COLUMN IF EXISTS n8n_execution_id;
