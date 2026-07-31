CREATE TABLE IF NOT EXISTS aiops_requests (
  request_id text PRIMARY KEY,
  slack_event_id text NOT NULL UNIQUE,
  team_id text,
  channel_id text NOT NULL,
  user_id text NOT NULL,
  message_ts text NOT NULL,
  thread_ts text,
  question text NOT NULL,
  status text NOT NULL DEFAULT 'received',
  last_error text,
  raw_payload jsonb NOT NULL,
  received_at timestamptz NOT NULL,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS aiops_requests_status_idx
  ON aiops_requests (status, received_at DESC);

-- When a request comes back needing clarification, the user answers in the
-- thread of their original message. The reply carries thread_ts equal to the
-- original message_ts, which is how the two are tied together.
ALTER TABLE aiops_requests
  ADD COLUMN IF NOT EXISTS parent_request_id text
  REFERENCES aiops_requests(request_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS aiops_requests_thread_idx
  ON aiops_requests (channel_id, message_ts, status);

CREATE TABLE IF NOT EXISTS aiops_dispatch_queue (
  id bigserial PRIMARY KEY,
  request_id text NOT NULL UNIQUE REFERENCES aiops_requests(request_id) ON DELETE CASCADE,
  attempts integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  locked_until timestamptz,
  dispatched_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS aiops_dispatch_due_idx
  ON aiops_dispatch_queue (next_attempt_at)
  WHERE dispatched_at IS NULL;

CREATE TABLE IF NOT EXISTS aiops_agent_runs (
  id bigserial PRIMARY KEY,
  request_id text NOT NULL REFERENCES aiops_requests(request_id) ON DELETE CASCADE,
  stage text NOT NULL,
  status text NOT NULL,
  model text,
  duration_ms integer,
  output jsonb,
  error text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS aiops_agent_runs_request_idx
  ON aiops_agent_runs (request_id, created_at);

CREATE TABLE IF NOT EXISTS aiops_tool_calls (
  id bigserial PRIMARY KEY,
  request_id text NOT NULL REFERENCES aiops_requests(request_id) ON DELETE CASCADE,
  tool_call_id text NOT NULL,
  tool_name text NOT NULL,
  purpose text NOT NULL,
  status text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (request_id, tool_call_id)
);

CREATE TABLE IF NOT EXISTS aiops_reports (
  request_id text PRIMARY KEY REFERENCES aiops_requests(request_id) ON DELETE CASCADE,
  parsed_request jsonb NOT NULL,
  evidence_package jsonb NOT NULL,
  rca_report jsonb NOT NULL,
  slack_markdown text NOT NULL,
  slack_channel_id text NOT NULL,
  slack_message_ts text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS aiops_system_errors (
  id bigserial PRIMARY KEY,
  request_id text REFERENCES aiops_requests(request_id) ON DELETE SET NULL,
  workflow_name text,
  execution_id text,
  last_node text,
  message text NOT NULL,
  details jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
