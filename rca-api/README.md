# LangGraph RCA service

This service now implements the live reasoning path behind n8n. n8n still owns
Slack ingress/delivery and audit persistence; the service owns request analysis,
the LangGraph investigation loop, live MCP calls and RCA writing.

## Runtime flow

```text
n8n
  -> POST /v1/investigations
  -> Question Analyzer (OpenAI structured output)
  -> Resolve hosts (Zabbix find_hosts)
  -> Shallow incident-event scan
  -> Hypothesis / observation LangGraph loop
  -> Zabbix, official Elasticsearch, or Wazuh MCP adapter
  -> Evidence package
  -> RCA Writer (OpenAI structured output)
  -> n8n audit persistence and Slack delivery
```

The API preserves the existing parsed-request, evidence-package and report
contracts. Every MCP call passes through the read-only allowlist and runtime
guards before a Streamable HTTP MCP session is opened. At the start of an
investigation, the service reads the live MCP tool catalogs, removes example
keywords, filters out tools outside the allowlist, and checkpoints the remaining
descriptions and input/output schemas in shared graph state. This preserves
`enum`, `pattern`, `format` and required-field constraints without steering the
planner with concrete example values. The graph state also contains shared
hosts, hypotheses, evidence, unknowns, budgets and trace data; model nodes
receive only the slice needed for their decision.

## HTTP API

- `GET /healthz`
- `GET /readyz`
- `POST /v1/investigations`

The investigation endpoint requires `X-AIOPS-Internal-Token`. Its body contains
the request envelope, optional prior clarification question, and the enabled
report-template catalog fetched by n8n. The response contains the selected
template, three stable output contracts, agent-run audit records and the graph
trace.

## Configuration

Required settings are validated at process startup:

- `AIOPS_INTERNAL_TOKEN`
- `OPENAI_API_KEY`
- `ZABBIX_MCP_URL`, `ZABBIX_MCP_AUTH_TOKEN`
- `OSS_ES_MCP_URL`
- `WAZUH_MCP_URL`, `WAZUH_MCP_AUTH_TOKEN`

The model names can be overridden with `RCA_QUESTION_MODEL`,
`RCA_INVESTIGATION_MODEL` and `RCA_WRITER_MODEL`. MCP and model deadlines use
`MCP_TIMEOUT_SECONDS` and `MODEL_TIMEOUT_SECONDS`.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
```

Run locally after setting the required environment variables:

```powershell
.\.venv\Scripts\aiops-rca-api.exe
```
