import asyncio
import json
from collections.abc import Mapping
from typing import Any

from fastapi.testclient import TestClient

from aiops_rca.api.app import create_app
from aiops_rca.api.models import InvestigationApiRequest, ReportTemplate
from aiops_rca.config.settings import Settings
from aiops_rca.schemas.investigation import Hypothesis, RequestEnvelope
from aiops_rca.schemas.parsed_request import ParsedRequest
from aiops_rca.schemas.report import Report, ReportSection
from aiops_rca.services.investigation import InvestigationService
from aiops_rca.services.model_contracts import (
    HypothesisPlan,
    HypothesisUpdateDecision,
    ObservationDecision,
    PhenomenonDecision,
    ToolCandidate,
)
from aiops_rca.tools.adapters.base import AdapterSet, McpAdapter
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY


class LiveFixtureTransport:
    def __init__(self, source: str) -> None:
        self.source = source
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.list_count = 0

    async def list_tools(self) -> list[dict[str, Any]]:
        self.list_count += 1
        if self.source == "zabbix":
            return [
                {
                    "name": "find_hosts",
                    "description": "Resolve monitored hosts.",
                    "inputSchema": {"type": "object"},
                },
                {
                    "name": "get_incident_events",
                    "description": "Read incident events in a bounded window.",
                    "inputSchema": {"type": "object"},
                },
                {
                    "name": "query_zabbix",
                    "description": "Run one allowed read-only API method.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "method": {
                                "type": "string",
                                "enum": ["host.get", "item.get"],
                                "examples": ["host.get"],
                            }
                        },
                    },
                },
            ]
        if self.source == "elasticsearch":
            return [
                {
                    "name": "search",
                    "description": "Search an index with Query DSL.",
                    "inputSchema": {
                        "type": "object",
                        "required": ["index", "query_body"],
                    },
                }
            ]
        return [
            {
                "name": "get_wazuh_alert_summary",
                "description": "Summarize alerts in a bounded window.",
                "inputSchema": {"type": "object"},
            }
        ]

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        arguments = dict(arguments)
        self.calls.append((tool_name, arguments))
        if self.source == "elasticsearch" and tool_name == "search":
            return {
                "hits": [{"message": "connection refused", "host": "node-alpha"}],
            }
        if self.source != "zabbix":
            raise AssertionError(f"unexpected {self.source} call: {tool_name}")
        if tool_name == "find_hosts":
            return {
                "hosts": [
                    {
                        "host_id": "10101",
                        "host": "node-alpha",
                        "name": "Node Alpha",
                        "status": "monitored",
                        "groups": [],
                    }
                ],
                "result_count": 1,
                "truncated": False,
            }
        if tool_name == "get_incident_events":
            return {
                "window": {
                    "from": arguments["time_from"],
                    "to": arguments["time_to"],
                },
                "events": [
                    {
                        "event_id": "20202",
                        "trigger_id": "30303",
                        "name": "service unavailable",
                        "severity": "high",
                        "started_at": "2026-08-13T00:00:00Z",
                        "recovered_at": None,
                    }
                ],
            }
        raise AssertionError(f"unexpected Zabbix call: {tool_name}")


class FixtureModel:
    def __init__(self, *, investigate_logs: bool = False) -> None:
        self.output_types: list[str] = []
        self.payloads: dict[str, Any] = {}
        self.investigate_logs = investigate_logs

    async def complete(
        self,
        *,
        model: str,
        output_type: type[Any],
        system_prompt: str,
        payload: object,
        reasoning_effort: str,
    ) -> Any:
        del model, system_prompt, reasoning_effort
        self.output_types.append(output_type.__name__)
        self.payloads[output_type.__name__] = payload
        # The analyzer's type is built per request, narrowing request_type to
        # the catalog it was handed, so this dispatches on the base rather than
        # on identity.
        if issubclass(output_type, ParsedRequest):
            request_id = payload["request_id"]
            question = payload["question"]
            return ParsedRequest(
                request_id=request_id,
                parse_status="ready",
                request_type="incident_rca",
                host_queries=["node-alpha"],
                anchor_time="2026-08-13T00:00:00Z",
                timezone="Asia/Seoul",
                incident_description="service became unavailable",
                incident_type_hint="service_stop",
                user_intent="identify the cause",
                initial_window_hint={"before_minutes": 30, "after_minutes": 30},
                allow_dynamic_expansion=True,
                ambiguities=[],
                original_question=question,
            )
        if output_type is PhenomenonDecision:
            return PhenomenonDecision(
                phenomenon=(
                    "A service-unavailable event was observed at "
                    "2026-08-13T00:00:00Z and had not recovered."
                ),
            )
        if output_type is HypothesisPlan:
            if self.investigate_logs:
                return HypothesisPlan(
                    hypotheses=[
                        Hypothesis(
                            id="h1",
                            statement="The service rejected a downstream connection.",
                        )
                    ],
                    stop_reason=None,
                )
            return HypothesisPlan(
                hypotheses=[],
                stop_reason="The shallow event evidence is sufficient for this fixture.",
            )
        if output_type is ObservationDecision:
            return ObservationDecision(
                question="Do logs show a downstream connection failure?",
                discriminates_hypothesis_ids=["h1"],
                expected_if_true=[],
                expected_if_false=[],
                temporal_scope="historical",
                required_effect="raw_log_evidence",
                candidates=[
                    ToolCandidate(
                        tool_name="search",
                        host_id="10101",
                        arguments_json=json.dumps(
                            {
                                "index": "logs-*",
                                "query_body": {"query": {"match_all": {}}},
                            }
                        ),
                    )
                ],
                generic_fallback_allowed=True,
                stop_reason=None,
            )
        if output_type is HypothesisUpdateDecision:
            return HypothesisUpdateDecision(
                updates=[],
                new_hypotheses=[],
                new_facts=[],
                stop_reason="The planned log observation was collected.",
            )
        if output_type is Report:
            return Report(
                title="Incident RCA",
                sections=[
                    ReportSection(
                        id="summary",
                        body="A service-unavailable event was observed.",
                    )
                ],
            )
        raise AssertionError(f"unexpected structured output: {output_type.__name__}")


class StaticService:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls = 0

    async def investigate(self, _request: InvestigationApiRequest) -> Any:
        self.calls += 1
        return self.response


def settings() -> Settings:
    return Settings(
        _env_file=None,
        aiops_internal_token="internal-secret",
        openai_api_key="test-openai-key",
        zabbix_mcp_url="http://zabbix-mcp/mcp",
        zabbix_mcp_auth_token="test-zabbix-token",
        oss_es_mcp_url="http://elasticsearch:8081/mcp",
        wazuh_mcp_url="http://wazuh-mcp/mcp",
        wazuh_mcp_auth_token="test-wazuh-token",
    )


def request() -> InvestigationApiRequest:
    return InvestigationApiRequest(
        request=RequestEnvelope(
            request_id="req-live-test",
            source="n8n",
            received_at="2026-08-13T00:01:00Z",
            timezone="Asia/Seoul",
            question="node-alpha 서비스 장애 원인을 조사해줘",
            metadata={"user_id": "test-user"},
        ),
        templates=[
            ReportTemplate(
                template_id="incident_rca",
                version=1,
                title="Incident RCA",
                description="Investigate an incident",
                collection={
                    "host_selector": {"mode": "from_question"},
                    "window": {"range": "anchor_relative"},
                    "aggregation": "raw",
                    "limits": {
                        "max_iterations": 3,
                        "max_tool_calls": 10,
                        "max_duration_seconds": 120,
                    },
                },
                output={
                    "guidance": "Use only packaged evidence.",
                    "sections": [
                        {
                            "id": "summary",
                            "heading": "Summary",
                            "instruction": "Summarize the incident.",
                            "required": True,
                        }
                    ],
                },
            )
        ],
    )


def build_fixture_service(
    *, investigate_logs: bool = False
) -> tuple[
    InvestigationService,
    LiveFixtureTransport,
    LiveFixtureTransport,
    FixtureModel,
]:
    zabbix_transport = LiveFixtureTransport("zabbix")
    elasticsearch_transport = LiveFixtureTransport("elasticsearch")
    adapters = AdapterSet(
        zabbix=McpAdapter(
            source="zabbix",
            registry=DEFAULT_TOOL_REGISTRY,
            transport=zabbix_transport,
        ),
        elasticsearch=McpAdapter(
            source="elasticsearch",
            registry=DEFAULT_TOOL_REGISTRY,
            transport=elasticsearch_transport,
        ),
        wazuh=McpAdapter(
            source="wazuh",
            registry=DEFAULT_TOOL_REGISTRY,
            transport=LiveFixtureTransport("wazuh"),
        ),
    )
    model = FixtureModel(investigate_logs=investigate_logs)
    return (
        InvestigationService(settings=settings(), model=model, adapters=adapters),
        zabbix_transport,
        elasticsearch_transport,
        model,
    )


def test_live_service_connects_models_graph_and_mcp_adapters():
    service, zabbix, elasticsearch, model = build_fixture_service()

    response = asyncio.run(service.investigate(request()))

    assert response.status == "completed"
    assert response.evidence_package is not None
    assert response.report is not None
    assert [name for name, _arguments in zabbix.calls] == [
        "find_hosts",
        "get_incident_events",
    ]
    assert zabbix.list_count == 1
    assert elasticsearch.list_count == 1
    assert elasticsearch.calls == []
    # The first entry is the per-request analyzer type, named for the catalog
    # it was bound to rather than for the stored contract.
    assert model.output_types == [
        "CatalogBoundParsedRequest",
        "PhenomenonDecision",
        "HypothesisPlan",
        "Report",
    ]
    assert [run.stage for run in response.agent_runs] == [
        "question_analyzer",
        "evidence_collector",
        "rca_writer",
    ]
    assert response.evidence_package.evidence[0].evidence_id == "zbx:event:20202"
    assert response.trace is not None
    assert response.trace.visited_nodes == [
        "resolve_hosts",
        "establish_phenomenon",
        "hypothesis_planner",
        "observation_planner",
        "tool_router",
        "evidence_package_builder",
    ]


def test_graph_can_use_elasticsearch_after_the_initial_host_and_event_scan():
    service, zabbix, elasticsearch, model = build_fixture_service(investigate_logs=True)

    response = asyncio.run(service.investigate(request()))

    assert response.status == "completed"
    assert [name for name, _arguments in zabbix.calls] == [
        "find_hosts",
        "get_incident_events",
    ]
    assert [name for name, _arguments in elasticsearch.calls] == ["search"]
    assert "ObservationDecision" in model.output_types
    assert "HypothesisUpdateDecision" in model.output_types
    catalog = model.payloads["ObservationDecision"]["tool_catalog"]
    query_zabbix = next(item for item in catalog if item["name"] == "query_zabbix")
    method_schema = query_zabbix["input_schema"]["properties"]["method"]
    assert method_schema["enum"] == ["host.get", "item.get"]
    assert "examples" not in method_schema
    assert response.evidence_package is not None
    assert {item.source for item in response.evidence_package.evidence} == {
        "zabbix",
        "elasticsearch",
    }


def test_http_api_requires_token_and_returns_the_stable_response_envelope():
    live_service, _zabbix, _elasticsearch, _model = build_fixture_service()
    completed = asyncio.run(live_service.investigate(request()))
    static = StaticService(completed)
    client = TestClient(create_app(settings=settings(), service=static))

    assert client.get("/healthz").json() == {"status": "ok"}
    assert (
        client.post(
            "/v1/investigations", json=request().model_dump(mode="json")
        ).status_code
        == 401
    )

    response = client.post(
        "/v1/investigations",
        json=request().model_dump(mode="json"),
        headers={"X-AIOPS-Internal-Token": "internal-secret"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["agent_runs"][1]["stage"] == "evidence_collector"
    assert static.calls == 1
