-- One Slack message must produce one request.
--
-- An app subscribed to both app_mention and message.channels receives a single
-- mention twice: two deliveries, two event ids, one message. The event id is
-- unique per delivery, so the existing key cannot recognise the pair and the
-- bot answers the same question twice. The channel and message timestamp
-- identify the message itself and do catch it.

-- Hand the survivor anything that pointed at a row about to be removed.
--
-- parent_request_id is ON DELETE SET NULL, so the delete below would otherwise
-- cut a continuation loose from the request it was answering. The two rows are
-- one Slack message, so the clarification a continuation answered was asked by
-- the survivor just as much as by its twin, and the link belongs on the row
-- that stays.
--
-- This matters most on the run that applies these files to a database that
-- predates them, where the delete fires for the first time against real
-- duplicates. 005 takes a continuation's thread anchor from its parent, and a
-- request whose parent was deleted has neither a parent nor any other record of
-- the anchor -- it would be left pointing at the wrong thread permanently. The
-- duplicates arose on requests that asked for clarification, which are exactly
-- the ones continuations descend from, so the overlap is the common case rather
-- than a corner of it.
--
-- No row can be handed to itself: a continuation and the request it answers are
-- different Slack messages, so they never share the partition key below.
WITH ranked AS (
  SELECT
    request_id,
    row_number() OVER duplicates AS rank,
    first_value(request_id) OVER duplicates AS survivor_id
  FROM aiops_requests AS request
  WINDOW duplicates AS (
    PARTITION BY channel_id, message_ts
    ORDER BY
      EXISTS (
        SELECT 1 FROM aiops_reports report
        WHERE report.request_id = request.request_id
      ) DESC,
      received_at,
      request_id
  )
)
UPDATE aiops_requests AS child
SET parent_request_id = ranked.survivor_id,
    updated_at = now()
FROM ranked
WHERE child.parent_request_id = ranked.request_id
  AND ranked.rank > 1;

-- Collapse the pairs already stored. Keep whichever row produced a report, and
-- otherwise the one that arrived first. The ordering matches the window above,
-- so the row kept here is the one children were just handed to.
WITH ranked AS (
  SELECT
    request_id,
    row_number() OVER (
      PARTITION BY channel_id, message_ts
      ORDER BY
        EXISTS (
          SELECT 1 FROM aiops_reports report
          WHERE report.request_id = request.request_id
        ) DESC,
        received_at,
        request_id
    ) AS rank
  FROM aiops_requests AS request
)
DELETE FROM aiops_requests
WHERE request_id IN (SELECT request_id FROM ranked WHERE rank > 1);

CREATE UNIQUE INDEX IF NOT EXISTS aiops_requests_message_uniq
  ON aiops_requests (channel_id, message_ts);
