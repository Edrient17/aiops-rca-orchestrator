# n8n contract inventory (historical)

Nothing described here still runs. This is the production path as it was before
the LangGraph cutover, kept because it is the only account of what the agent
chain did. n8n has since been removed outright and ingress took over the Slack
and persistence work attributed to it below; the workflow files and the
generator that wrote them are gone with it. Read every sentence in the past
tense -- where one says "current" or "still", it means current as of the
cutover.

## Input and dispatch

1. ingress verifies Slack signatures at `POST /slack/events`.
2. it stores the request and an outbox row before acknowledging Slack.
3. the dispatcher posts the stored payload to n8n's `/webhook/aiops-process`
   with `X-AIOPS-Internal-Token`.
4. `Normalize Request` produces the current internal request shape, including
   `request_id`, `question`, `received_at`, Slack IDs and optional clarification
   parent data.

The RCA API envelope intentionally keeps only the diagnostic inputs and
puts Slack-specific values under `metadata`.

## Agent stages

| n8n node | Input | Output contract | Migration owner |
| --- | --- | --- | --- |
| `Question Analyzer` | normalized request + report catalog | `parsed-request.schema.json` | LangGraph API |
| `Evidence Collector` | parsed request + selected template collection/window/limits | `evidence-package.schema.json` | LangGraph collector graph |
| `RCA Writer` | parsed request + evidence package + template sections | `report.schema.json` | LangGraph API |

The three JSON Schema files in `../../schemas` were the compatibility source of
truth during the migration, and still hold the contracts named above.

## MCP boundary

| n8n tool node | URL | n8n authentication | Notes |
| --- | --- | --- | --- |
| `Zabbix MCP Tools` | `ZABBIX_MCP_URL` | Bearer credential | structured read-only investigation tools |
| `Elasticsearch Query Tools` | `OSS_ES_MCP_URL` | none | official generic `search`/`esql` server |
| `Wazuh MCP Tools` | `WAZUH_MCP_URL` | Bearer credential | audit plus current process/port tools |

LangGraph nodes call these through the shared Streamable HTTP adapters. They do
not construct MCP HTTP requests independently.

## Output and audit

At the point this inventory describes, n8n retained:

- the first Slack acknowledgement,
- clarification and unsupported replies,
- final Slack formatting and delivery,
- integration-level error notification,
- writes to ingress internal APIs for request status, execution linkage, agent
  runs and the final report.

LangGraph owns diagnostic MCP failures. An MCP timeout is state/unknown data and
does not by itself become an HTTP 5xx response.

## Cutover outcome

The cutover is complete and the boundary it describes no longer has two sides:

- the legacy reasoning branch, its three agents and their models, parsers and
  MCP client nodes were removed, along with the `RCA_EXECUTION_MODE` flag that
  selected between them;
- every accepted request now goes to the internal RCA HTTP API;
- n8n kept Slack and persistence at that point, while the API took model
  calls, LangGraph state and live MCP sessions. n8n was removed afterwards and
  ingress absorbed what it had kept.
