# LangGraph RCA service

This directory is the migration target for diagnostic reasoning that currently
lives inside the n8n workflow. It is deliberately not wired into production
yet. The current n8n path remains the control path while this service is built
and compared in shadow runs.

## Current migration inventory

The existing workflow contracts being preserved are:

- `Question Analyzer` produces `../schemas/parsed-request.schema.json`.
- `Evidence Collector` consumes the parsed request and a selected report
  template, calls the Zabbix, Elasticsearch and Wazuh MCP servers, and produces
  `../schemas/evidence-package.schema.json`.
- `RCA Writer` consumes only that evidence package and the selected template,
  and produces `../schemas/report.schema.json`.
- n8n owns Slack delivery, clarification delivery and integration-level error
  notification.
- ingress and PostgreSQL remain the audit boundary for requests, agent runs,
  tool calls, reports and feedback.

## Scope of this first migration slice

- Pydantic models matching the three existing JSON contracts
- explicit `InvestigationState`
- MCP adapter interfaces and normalized execution results
- read-only tool registry and deterministic guards
- injectable LangGraph collector skeleton with a conditional investigation loop
- mock-fixture unit tests

There is intentionally no HTTP API, live MCP transport, model call, prompt
rewrite, or n8n workflow change in this slice.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
```
