-- Operator feedback on published RCA reports.
--
-- A report is posted to Slack and its channel and message ts are already kept
-- in aiops_reports, so a reaction event carries enough to identify which
-- investigation is being judged without any new correlation id.

CREATE TABLE IF NOT EXISTS aiops_report_feedback (
  id bigserial PRIMARY KEY,
  request_id text NOT NULL REFERENCES aiops_requests(request_id) ON DELETE CASCADE,
  user_id text NOT NULL,
  -- The raw Slack emoji name, kept so the mapping can be revised later without
  -- losing what was actually pressed.
  reaction text NOT NULL,
  label text NOT NULL CHECK (label IN ('correct', 'partial', 'incorrect')),
  created_at timestamptz NOT NULL DEFAULT now(),
  -- One row per person per emoji, so removing that emoji deletes exactly what
  -- adding it created and a Slack retry cannot double count.
  UNIQUE (request_id, user_id, reaction)
);

CREATE INDEX IF NOT EXISTS aiops_report_feedback_label_idx
  ON aiops_report_feedback (label, created_at DESC);

-- A reaction says the conclusion was wrong but not what the truth was. The
-- correction is written as a reply in the report thread and lands here.
CREATE TABLE IF NOT EXISTS aiops_report_notes (
  id bigserial PRIMARY KEY,
  request_id text NOT NULL REFERENCES aiops_requests(request_id) ON DELETE CASCADE,
  user_id text NOT NULL,
  slack_message_ts text NOT NULL,
  note text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  -- Slack redelivers events on a slow ack; the message ts makes that harmless.
  UNIQUE (request_id, slack_message_ts)
);

CREATE INDEX IF NOT EXISTS aiops_report_notes_request_idx
  ON aiops_report_notes (request_id, created_at);

-- The training/RAG record: what was asked, what evidence was gathered, what the
-- agent concluded, and whether an operator agreed. One row per report.
CREATE OR REPLACE VIEW aiops_labeled_dataset AS
SELECT
  report.request_id,
  request.question,
  request.received_at,
  report.parsed_request,
  report.evidence_package,
  report.rca_report,
  report.slack_markdown,
  verdict.label,
  verdict.labeled_by,
  verdict.labeled_at,
  correction.notes
FROM aiops_reports AS report
JOIN aiops_requests AS request USING (request_id)
LEFT JOIN LATERAL (
  SELECT
    -- Worst verdict wins. One reviewer calling a report wrong outweighs
    -- another calling it right, which is the safe direction for a dataset.
    (ARRAY['incorrect', 'partial', 'correct'])[
      min(CASE feedback.label
            WHEN 'incorrect' THEN 1
            WHEN 'partial' THEN 2
            ELSE 3
          END)
    ] AS label,
    array_agg(DISTINCT feedback.user_id) AS labeled_by,
    max(feedback.created_at) AS labeled_at
  FROM aiops_report_feedback AS feedback
  WHERE feedback.request_id = report.request_id
) AS verdict ON true
LEFT JOIN LATERAL (
  SELECT array_agg(note.note ORDER BY note.created_at) AS notes
  FROM aiops_report_notes AS note
  WHERE note.request_id = report.request_id
) AS correction ON true;
