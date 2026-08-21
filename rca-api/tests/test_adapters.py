import asyncio
from collections.abc import Mapping
from typing import Any

from aiops_rca.tools.adapters.base import McpAdapter, describe_failure
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



class TestSayingWhatActuallyFailed:
    """A failure the planner can act on, rather than one that only happened.

    Every ES|QL failure reached the investigation as the same sentence --
    "unhandled errors in a TaskGroup (1 sub-exception)" -- because the MCP
    client runs its request inside a TaskGroup and that is what str() of an
    ExceptionGroup says. A syntax error, a field that does not exist and a dead
    connection were indistinguishable, so a live run retried a broken query
    unchanged and the report concluded the tooling had failed.
    """

    def test_the_cause_comes_out_of_the_group(self):
        group = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [ValueError("Unknown column [nope.field]")],
        )
        assert describe_failure(group) == "Unknown column [nope.field]"

    def test_a_group_of_several_names_them_all(self):
        group = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [ValueError("first"), RuntimeError("second")],
        )
        assert describe_failure(group) == "first; second"

    def test_nesting_does_not_hide_it(self):
        inner = ExceptionGroup("inner", [ValueError("the real one")])
        assert describe_failure(ExceptionGroup("outer", [inner])) == "the real one"

    def test_an_ordinary_exception_is_unchanged(self):
        assert describe_failure(ConnectionError("MCP unavailable")) == "MCP unavailable"

    def test_a_silent_exception_still_says_something(self):
        # An empty message used to become an empty error, which reads in the
        # report as a call that failed for no reason at all.
        assert describe_failure(TimeoutError()) == "TimeoutError"

    def test_a_group_of_silent_exceptions_says_something_too(self):
        group = ExceptionGroup("boom", [TimeoutError()])
        assert describe_failure(group) == "TimeoutError"
