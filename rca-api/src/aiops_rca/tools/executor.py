"""Graph-facing executor that keeps MCP implementation details out of nodes."""

from aiops_rca.schemas.investigation import PlannedToolCall
from aiops_rca.tools.adapters.base import AdapterSet
from aiops_rca.tools.registry import RoutingContext, ToolRegistry
from aiops_rca.tools.result import ToolExecutionResult


class ToolExecutor:
    def __init__(self, adapters: AdapterSet, registry: ToolRegistry) -> None:
        self.adapters = adapters
        self.registry = registry

    async def execute(
        self,
        planned: PlannedToolCall,
        context: RoutingContext,
    ) -> ToolExecutionResult:
        policy = self.registry.get(planned.tool_name)
        adapter = self.adapters.for_source(policy.source)
        return await adapter.execute(
            planned.tool_name,
            planned.arguments,
            context,
        )
