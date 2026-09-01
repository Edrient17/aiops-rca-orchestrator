"""Transport-independent, mockable MCP adapter layer."""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from aiops_rca.sources import SOURCES, ToolSource
from aiops_rca.tools.registry import (
    RoutingContext,
    ToolPolicy,
    ToolRegistry,
    apply_window_policy,
)
from aiops_rca.tools.result import ToolExecutionResult


class McpTransport(Protocol):
    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the server's live tool metadata and JSON Schemas."""

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        """Call one MCP tool and return its decoded structured result."""


class McpAdapter:
    """Validate, execute, classify and trace one source's MCP calls."""

    def __init__(
        self,
        *,
        source: ToolSource,
        registry: ToolRegistry,
        transport: McpTransport,
        timeout_seconds: float = 120,
    ) -> None:
        self.source = source
        self.registry = registry
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    async def list_tools(self) -> list[dict[str, Any]]:
        """Read the live catalog through the same bounded transport boundary."""

        return await asyncio.wait_for(
            self.transport.list_tools(),
            timeout=self.timeout_seconds,
        )

    async def execute(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        context: RoutingContext,
        *,
        tool_call_id: str | None = None,
    ) -> ToolExecutionResult:
        policy = self.registry.validate_call(tool_name, arguments, context)
        if policy.source != self.source:
            raise ValueError(
                f"{tool_name} belongs to {policy.source}, not adapter {self.source}",
            )
        # Applied here rather than in the router because the router is not the
        # only caller: the phenomenon scan builds its own arguments and reaches
        # the adapter directly, and it is the one that asks for a whole month.
        arguments = apply_window_policy(
            policy, arguments, context.declared_window_policy
        )

        call_id = tool_call_id or f"call-{uuid4()}"
        started_at = datetime.now(UTC)
        try:
            response = await asyncio.wait_for(
                self.transport.call_tool(tool_name, arguments),
                timeout=self.timeout_seconds,
            )
            status, error = classify_result(policy, response)
        except TimeoutError:
            response = None
            status = "error"
            error = f"tool call timed out after {self.timeout_seconds:g} seconds"
        # A transport implementation may surface library-specific exception
        # types. This boundary intentionally normalizes every such failure into
        # investigation state instead of leaking it as an API-level failure.
        except Exception as exception:
            response = None
            status = "error"
            error = describe_failure(exception)

        return ToolExecutionResult(
            tool_call_id=call_id,
            tool_name=tool_name,
            source=self.source,
            status=status,
            request=dict(arguments),
            response=response,
            error=error,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )


def describe_failure(exception: BaseException) -> str:
    """What actually went wrong, rather than what the concurrency helper called it.

    An MCP client that runs its request inside a TaskGroup raises an
    ExceptionGroup, and `str()` of one is "unhandled errors in a TaskGroup
    (1 sub-exception)" -- the same sentence for an ES|QL syntax error, a field
    that does not exist, and a dead connection. That sentence is what reached
    the planner, the unknowns and the report, so a live investigation retried a
    broken query unchanged and then reported that the tooling had failed.

    The causes are one level down, or several. Unwrapping them costs nothing
    and is the difference between a message a planner can act on and a message
    that only says something happened.
    """
    if isinstance(exception, BaseExceptionGroup):
        inner = "; ".join(
            describe_failure(item) for item in exception.exceptions
        )
        if inner:
            return inner
    text = str(exception).strip()
    return text or exception.__class__.__name__


def classify_result(policy: ToolPolicy, response: Any) -> tuple[str, str | None]:
    """Classify meaning without interpreting the observation as root-cause evidence."""

    if isinstance(response, Mapping):
        explicit_error = response.get("error")
        if explicit_error:
            return "error", _error_text(explicit_error)
        if response.get("isError") is True:
            return "error", _error_text(response.get("message") or response)

        quality = response.get("data_quality")
        if isinstance(quality, Mapping):
            if quality.get("empty_because_filtered") is not None:
                return "filtered_empty", None
            if quality.get("partial") is True:
                return "partial", None
        if response.get("partial") is True:
            return "partial", None

        for field in policy.result_list_fields:
            value = response.get(field)
            if isinstance(value, list):
                return ("empty", None) if len(value) == 0 else ("ok", None)
            if isinstance(value, Mapping) and isinstance(value.get(field), list):
                nested = value[field]
                return ("empty", None) if len(nested) == 0 else ("ok", None)
        if len(response) == 0:
            return "empty", None

    if isinstance(response, list):
        return ("empty", None) if len(response) == 0 else ("ok", None)
    if response is None:
        return "empty", None
    if isinstance(response, str) and response.strip().lower().startswith("no "):
        return "empty", None
    return "ok", None


def _error_text(value: Any) -> str:
    if isinstance(value, str):
        return value[:10_000]
    return repr(value)[:10_000]


class AdapterSet:
    """The adapters, keyed by source rather than named one per field.

    Named fields meant a new MCP server had to be added twice here -- once as a
    field and once in the lookup -- and the second was easy to miss.
    """

    def __init__(self, adapters: Mapping[ToolSource, McpAdapter] | None = None, **named: McpAdapter) -> None:
        merged: dict[str, McpAdapter] = dict(adapters or {})
        merged.update(named)
        missing = sorted(set(SOURCES) - set(merged))
        if missing:
            raise ValueError(f"no adapter for source(s): {', '.join(missing)}")
        self.adapters = merged

    def for_source(self, source: ToolSource) -> McpAdapter:
        return self.adapters[source]

    @property
    def zabbix(self) -> McpAdapter:
        """Named because host resolution and the phenomenon scan are Zabbix's."""

        return self.adapters["zabbix"]
