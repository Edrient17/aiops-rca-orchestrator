-- One Slack message must produce one request.
--
-- An app subscribed to both app_mention and message.channels receives a single
-- mention twice: two deliveries, two event ids, one message. The event id is
-- unique per delivery, so the existing key cannot recognise the pair and the
-- bot answers the same question twice. The channel and message timestamp
-- identify the message itself and do catch it.

-- Collapse the pairs already stored. Keep whichever row produced a report, and
-- otherwise the one that arrived first.
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
