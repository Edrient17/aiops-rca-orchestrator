import asyncio
from collections.abc import Mapping
from typing import Any

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

