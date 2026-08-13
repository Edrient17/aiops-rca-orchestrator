import asyncio
from collections.abc import Mapping
from typing import Any

from conftest import make_state

from aiops_rca.graph.deterministic_nodes import ResolveHostsNode
from aiops_rca.tools.adapters.base import McpAdapter
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY, RoutingContext


class QueueTransport:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((tool_name, dict(arguments)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def adapter(transport: QueueTransport) -> McpAdapter:
    return McpAdapter(
        source="zabbix",
        registry=DEFAULT_TOOL_REGISTRY,
        transport=transport,
        timeout_seconds=1,
    )


def test_adapter_classifies_transport_error_without_turning_it_into_empty_data():
    result = asyncio.run(
        adapter(QueueTransport(ConnectionError("MCP unavailable"))).execute(
            "find_hosts",
            {"query": "host-a"},
            RoutingContext(),
        ),
    )
    assert result.status == "error"
    assert result.error == "MCP unavailable"
    assert result.response is None


def test_resolve_hosts_accepts_one_exact_match(fixture_json):
    node = ResolveHostsNode(
        adapter(QueueTransport(fixture_json("zabbix/find_hosts_exact.json")))
    )
    update = asyncio.run(node(make_state()))

    assert [(host.host, host.host_id) for host in update["hosts"]] == [
        ("vm-java-docker-2", "11094"),
    ]
    assert update["unresolved_hosts"] == []
    assert update["tool_call_count"] == 1
    assert update["stop_reason"] is None


def test_resolve_hosts_never_guesses_between_ambiguous_matches(fixture_json):
    node = ResolveHostsNode(
        adapter(QueueTransport(fixture_json("zabbix/find_hosts_ambiguous.json")))
    )
    update = asyncio.run(node(make_state(host_queries=["payment"])))

    assert update["hosts"] == []
    assert update["unresolved_hosts"] == ["payment"]
    assert update["unknowns"][0].code == "host_ambiguous"
    assert update["stop_reason"] == "no host could be resolved for investigation"


def test_resolve_hosts_stops_cleanly_when_budget_is_exhausted(fixture_json):
    transport = QueueTransport(fixture_json("zabbix/find_hosts_exact.json"))
    node = ResolveHostsNode(adapter(transport))
    update = asyncio.run(
        node(
            make_state(
                host_queries=["vm-java-docker-2", "second-host"],
                limits={
                    "max_tool_calls": 1,
                    "max_iterations": 10,
                    "max_duration_seconds": 300,
                },
            ),
        ),
    )
    assert len(transport.calls) == 1
    assert update["unresolved_hosts"] == ["second-host"]
    assert update["unknowns"][0].code == "host_resolution_budget_exhausted"
