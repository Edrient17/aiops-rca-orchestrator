# LangGraph migration slices (historical)

The plan the migration was carried out under, kept as the record of how the
cutover was staged. All six slices landed. It was written while n8n was still
the Slack ingress/egress and audit boundary, and each slice was shaped to leave
the production workflow usable -- neither is true any more: n8n was removed
after slice 6 and ingress took over what it held. The slice text below is left
as written.

## Slice 1 — contracts and deterministic graph shell (implemented)

- Mirror the existing parsed request, evidence package, and report contracts in
  strict Pydantic models.
- Define `InvestigationState`, budgets, stop reasons, host resolution, tool
  policy, adapter boundaries, result classification, and evidence normalization.
- Compile the complete LangGraph topology with mock nodes and checkpointing.
- Do not expose an HTTP route or change an n8n workflow.

Exit criteria: mock graph tests, registry guard tests, JSON Schema compatibility,
and existing TypeScript regressions pass.

## Slice 2 — request analysis service (core path implemented)

- Implement the Question Analyzer as a structured-output LangGraph node.
- Add a small internal API endpoint with correlation and idempotency keys.
- Validate every model response against `ParsedRequest`; retry only repairable
  contract failures.
- Keep n8n on the current analyzer while replay tests compare both outputs.

Exit criteria: golden request fixtures cover Korean/English questions, explicit
and relative time ranges, missing hosts, and malformed model output.

## Slice 3 — diagnostic collection loop (core path implemented)

- Implement phenomenon establishment, hypothesis planning, observation planning,
  tool execution, hypothesis updates, and stop guards.
- Connect the existing Zabbix, official Elasticsearch, and Wazuh MCP transports
  through the allowlisted adapters.
- Preserve error, empty, filtered-empty, and partial results as distinct states.

Exit criteria: fixture-driven investigations prove ambiguity handling, historical
query protection, generic-search fallback policy, budget stops, and deterministic
evidence IDs.

## Slice 4 — evidence package and RCA report (core path implemented)

- Build the final evidence package deterministically from state.
- Implement the RCA Writer against the existing report contract.
- Checkpoint graph state by investigation ID and return final artifacts to n8n
  for persistence.
- Expose the complete internal investigation API without changing Slack routing.

Exit criteria: end-to-end API tests validate both JSON Schemas and resumability.

## Slice 5 — n8n integration path (implemented, default off)

- Add a feature-flagged branch in the existing workflow that sends the normalized
  request and template catalog to the LangGraph API.
- Persist returned Agent runs and, for completed investigations, tool-call
  records and final artifacts through the same ingress APIs as the legacy branch.
- Keep the legacy branch authoritative by default.

Exit criteria: an agreed replay/canary set has no contract regressions, no
write-capable tool calls, and acceptable latency/cost/error rates.

## Slice 6 — controlled cutover (complete)

- Traffic ran on the API behind the `RCA_EXECUTION_MODE` flag until the path had
  proven itself on both report kinds.
- The n8n reasoning nodes and the flag were then removed together: keeping a
  branch nothing could reach meant carrying a second, drifting copy of the
  prompts and schemas. Rollback became a redeploy of the previous workflow
  rather than an environment change.
- n8n retained Slack ingress/egress and workflow-level operations, until it was
  removed in turn and ingress absorbed both.

Exit criteria: production SLOs and RCA quality gates hold through the rollback
window, and the legacy reasoning path can be retired explicitly.
