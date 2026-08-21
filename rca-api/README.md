# LangGraph RCA service

This service owns the whole reasoning path: request analysis, the investigation
graph, every model call and every live MCP session. The ingress service owns
Slack ingress and delivery, the report-template catalog, and audit persistence —
it does not call a model or an MCP server.

## Runtime flow

```text
ingress
  -> POST /v1/investigations
  -> Question Analyzer          (structured output, bound to the template catalog)
  -> Resolve hosts              (Zabbix find_hosts)
  -> Establish phenomenon       (event scan across every resolved host)
  -> Coverage sweep             (what the report's sections declared)
  -> Hypothesis / observation loop
       planner -> router -> executor -> normalizer -> updater -> stop guard
  -> Coverage sweep             (again, if the loop stopped with a gap)
  -> Evidence package
  -> RCA Writer                 (structured output, one section per template id)
  -> ingress audit persistence and Slack delivery
```

Two things in that loop are worth knowing before reading the code.

**The loop stops on a reasoning question** — is there another observation that
would discriminate between the surviving hypotheses. That is not the same
question as whether the report can be written, which is why the coverage sweep
sits on both sides of it.

**Nothing the model produces is trusted as a reference.** Report kinds, routable
effects, hypothesis ids and evidence ids are all bound to the values that exist
at that moment, so an invented one cannot be represented rather than being
caught later.

## Evaluating the writer

The suite proves the machinery is right. It never proved the answers were: a
report saying twenty-five of a list of twenty-six passed all of it.
`evals/properties.py` judges a finished report against the evidence it was
written from -- counts that the cited evidence does not count, citations to
nothing, an omission not passed on, unknowns reported as no limitations. None of
them needs the right answer, which is what lets them run against infrastructure
whose right answer changes hourly.

`evals/harness.py` re-runs the writer over evidence packages this service really
collected, so an experiment measures the writer and nothing else. Export them
from the orchestrator host (the query is in `evals/run.py`), then:

```powershell
.\.venv\Scripts\python -m aiops_rca.evals.run baseline exports.jsonl
.\.venv\Scripts\python -m aiops_rca.evals.run upload   exports.jsonl
.\.venv\Scripts\python -m aiops_rca.evals.run evaluate exports.jsonl --limit 5
```

`baseline` scores the reports that already shipped and needs nothing but the
file -- it is the floor an experiment is compared against, and several of those
reports are the defects the checks were written from. `upload` and `evaluate`
need `LANGSMITH_API_KEY`; `evaluate` calls the writer model once per example, so
try it with `--limit` first.

Rows from before the current schemas, and report kinds since retired, are
counted and explained rather than dropped or raised on.

## Log store facts in the prompt

`prompts/log_queries.md` is composed into the three nodes that query logs. It
names the index pattern, the fields that carry the host and the message, and
which of three ways of matching text actually works. Every claim in it was
measured, and every claim in it can go stale: this deployment has three hosts
and a demo index template, and `host.name` in particular inverts between an ECS
mapping and this one.

A stale prompt is worse than none — the model follows it and names a field that
is not there — so re-derive it whenever the store changes:

```bash
python -m aiops_rca.evals.log_store probe
```

and verify the claims still hold, which exits non-zero when they do not:

```bash
python -m aiops_rca.evals.log_store check
```

`check` reads the claims out of the prompt rather than keeping a copy, so the
prompt stays the only place they are written. It needs the cluster, so CI runs
the unit tests around it but not the check itself.

## Collector graph

[docs/collector-graph.md](docs/collector-graph.md) draws the node topology and
the conditional routes. It is generated from the compiled graph, not drawn by
hand:

```powershell
.\.venv\Scripts\python -m aiops_rca.graph.diagram > docs/collector-graph.md
```

`tests/test_graph_diagram.py` compares it against the graph on every run, so a
route changed without regenerating it fails the suite rather than leaving a
diagram of last month's pipeline. Drawing needs no model, MCP session or
credentials, which is what lets it be a test instead of a chore.

For watching an actual run rather than the shape, use the LangSmith trace --
tracing is already wired below, and the graph is owned by this service rather
than by a `langgraph dev` server.

## Section evidence contract

A report template's sections declare what they are written from, using the tool
registry's effect names. See the orchestrator README for the template side; the
enforcement lives here.

| Where | What it does |
| --- | --- |
| `services/template_contract.py` | Reads the declarations; rejects one no tool can produce |
| `tools/coverage.py` | Which effects have been observed, and recipes to collect the rest |
| `graph/coverage_nodes.py` | Runs those recipes; records what it could not collect |
| `graph/routing.py` | Will not finish a run while a declared effect is unattempted |
| `services/investigation.py` | Marks unfillable sections; sends uncited events to limitations |

Coverage means having **looked**, not having found. An event query that returns
nothing covers `incident_events`: the window was searched and the absence is the
answer. Only an error leaves an effect uncovered.

## Evidence sources

`sources.py` describes each MCP server once — its settings fields, its generic
evidence prefix, and every evidence-id prefix it may produce. Transports,
adapters, the tool-catalog loader, the normalizer's prefix lookup and the
evidence-id pattern are all derived from it.

Adding a server is documented in the orchestrator README under
"MCP 서버 추가하기".

## HTTP API

- `GET /healthz`
- `GET /readyz`
- `POST /v1/investigations`

The investigation endpoint requires `X-AIOPS-Internal-Token`. Its body carries
the request envelope, an optional prior clarification question, and the enabled
report-template catalog the ingress service holds. The response carries the selected
template, the three stable output contracts, agent-run audit records and the
graph trace.

## Configuration

Validated at process startup:

- `AIOPS_INTERNAL_TOKEN`
- `OPENAI_API_KEY`
- `ZABBIX_MCP_URL`, `ZABBIX_MCP_AUTH_TOKEN`
- `OSS_ES_MCP_URL`
- `WAZUH_MCP_URL`, `WAZUH_MCP_AUTH_TOKEN`

Model names: `RCA_QUESTION_MODEL`, `RCA_INVESTIGATION_MODEL`,
`RCA_WRITER_MODEL`. Deadlines: `MCP_TIMEOUT_SECONDS`, `MODEL_TIMEOUT_SECONDS`.

Tracing is off unless `LANGSMITH_TRACING` is true **and** `LANGSMITH_API_KEY` is
set, so a deployment without LangSmith carries no tracing in its request path.
LangGraph traces its own topology; the OpenAI client is wrapped separately
because the model calls go through the SDK rather than a LangChain runnable.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check src tests
```

Run locally after setting the required environment variables:

```powershell
.\.venv\Scripts\aiops-rca-api.exe
```

`docs/` holds two records of the migration from the n8n agent chain. Both that
cutover and the removal of n8n itself are complete; the files are kept because
they are the only description of what it used to do.
