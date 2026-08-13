# LangGraph migration slices

The migration keeps n8n as the Slack ingress/egress and audit boundary until the
LangGraph path has passed shadow comparisons. Each slice is independently
deployable and must leave the production workflow usable.

## Slice 1 — contracts and deterministic graph shell (this change)

- Mirror the existing parsed request, evidence package, and report contracts in
  strict Pydantic models.
- Define `InvestigationState`, budgets, stop reasons, host resolution, tool
  policy, adapter boundaries, result classification, and evidence normalization.
- Compile the complete LangGraph topology with mock nodes and checkpointing.
- Do not expose an HTTP route or change an n8n workflow.

Exit criteria: mock graph tests, registry guard tests, JSON Schema compatibility,
and existing TypeScript regressions pass.

## Slice 2 — request analysis service

- Implement the Question Analyzer as a structured-output LangGraph node.
- Add a small internal API endpoint with correlation and idempotency keys.
- Validate every model response against `ParsedRequest`; retry only repairable
  contract failures.
- Keep n8n on the current analyzer while replay tests compare both outputs.

Exit criteria: golden request fixtures cover Korean/English questions, explicit
and relative time ranges, missing hosts, and malformed model output.

## Slice 3 — diagnostic collection loop

- Implement phenomenon establishment, hypothesis planning, observation planning,
  tool execution, hypothesis updates, and stop guards.
- Connect the existing Zabbix, official Elasticsearch, and Wazuh MCP transports
  through the allowlisted adapters.
- Preserve error, empty, filtered-empty, and partial results as distinct states.

Exit criteria: fixture-driven investigations prove ambiguity handling, historical
query protection, generic-search fallback policy, budget stops, and deterministic
evidence IDs.

## Slice 4 — evidence package and RCA report

- Build the final evidence package deterministically from state.
- Implement the RCA Writer against the existing report contract.
- Persist checkpoints and final artifacts by thread/correlation ID.
- Expose the complete internal investigation API without changing Slack routing.

Exit criteria: end-to-end API tests validate both JSON Schemas and resumability.

## Slice 5 — n8n shadow path

- Add a separate n8n workflow that sends the same normalized request to the
  LangGraph API without posting its report to Slack.
- Record latency, tool calls, stop reason, evidence overlap, and report contract
  validity for current and candidate paths.
- Keep the current workflow authoritative.

Exit criteria: an agreed replay/canary set has no contract regressions, no
write-capable tool calls, and acceptable latency/cost/error rates.

## Slice 6 — controlled cutover

- Route a small canary percentage to LangGraph while retaining the legacy path as
  an operational fallback.
- Increase traffic only after audit and quality gates pass.
- Remove n8n reasoning nodes after the rollback window; retain n8n for Slack
  ingress/egress and workflow-level operations unless a later decision changes
  that boundary.

Exit criteria: production SLOs and RCA quality gates hold through the rollback
window, and the legacy reasoning path can be retired explicitly.
