-- What each kind of request should gather, and how its report should read.
--
-- The pipeline has one shape baked into it: an incident RCA over one host. A
-- monthly capacity summary wants different metrics, a different window and a
-- different document, and encoding each new kind as another branch in the
-- workflow would mean a redeploy per document type. These rows hold that part
-- instead, so an operator adds a kind by writing a row.
--
-- collection answers "what should the Evidence Collector go and get", output
-- answers "how should the RCA Writer lay the report out". They live in one
-- table rather than two because they are two faces of one document kind and
-- always change together; split across tables they could disagree.
--
-- Neither column holds a JSON Schema. The agents' output contract is fixed and
-- lives with the workflow, because n8n's structured output parser takes a
-- static schema only. What varies here is the prompt, which does accept an
-- expression, so a template steers the model without the workflow changing.
CREATE TABLE IF NOT EXISTS aiops_report_templates (
  -- Goes into prompts and URLs, so it is kept to a plain identifier rather
  -- than anything needing escaping.
  template_id text PRIMARY KEY
    CHECK (template_id ~ '^[a-z][a-z0-9_]{2,63}$'),
  -- Bumped only when the content actually changes, so re-sending an unchanged
  -- template does not inflate the history.
  version integer NOT NULL DEFAULT 1,
  -- Retires a kind without deleting it, which would strand the reports that
  -- name it.
  enabled boolean NOT NULL DEFAULT true,
  title text NOT NULL,
  -- Read by the question analyzer to decide which kind a question is. This is
  -- the field that makes a template discoverable, so it describes *when to
  -- pick this*, not what the document contains.
  description text NOT NULL,
  collection jsonb NOT NULL,
  output jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS aiops_report_templates_enabled_idx
  ON aiops_report_templates (template_id)
  WHERE enabled;

-- Every version a template has ever had.
--
-- These rows are editable at runtime by design, which means a report published
-- last month was shaped by a template that may no longer exist in that form.
-- Without this a stored report could not be explained -- the same reason the
-- workflow records which model wrote each stage.
--
-- Deliberately no foreign key to aiops_report_templates: deleting a template
-- must not erase the history of reports that were produced from it.
CREATE TABLE IF NOT EXISTS aiops_report_template_versions (
  template_id text NOT NULL,
  version integer NOT NULL,
  enabled boolean NOT NULL,
  title text NOT NULL,
  description text NOT NULL,
  collection jsonb NOT NULL,
  output jsonb NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (template_id, version)
);
