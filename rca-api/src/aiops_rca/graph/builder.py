"""Collector graph topology; node implementations remain independently injectable."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph

from aiops_rca.graph.routing import (
    route_after_coverage_sweep,
    route_after_host_resolution,
    route_after_stop_guard,
    route_after_tool_router,
)
from aiops_rca.graph.state import InvestigationState

Node = Callable[[InvestigationState], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class CollectorNodes:
    """Small node contracts make every reasoning stage independently testable."""

    resolve_hosts: Node
    establish_phenomenon: Node
    coverage_sweep: Node
    hypothesis_planner: Node
    observation_planner: Node
    tool_router: Node
    tool_executor: Node
    evidence_normalizer: Node
    hypothesis_updater: Node
    stop_guard: Node
    evidence_package_builder: Node


def build_collector_graph(
    nodes: CollectorNodes,
    *,
    checkpointer: Any | None = None,
):
    """Compile the initial sequential collector loop with optional checkpointing."""

    builder = StateGraph(InvestigationState)
    builder.add_node("resolve_hosts", nodes.resolve_hosts)
    builder.add_node("establish_phenomenon", nodes.establish_phenomenon)
    builder.add_node("coverage_sweep", nodes.coverage_sweep)
    builder.add_node("hypothesis_planner", nodes.hypothesis_planner)
    builder.add_node("observation_planner", nodes.observation_planner)
    builder.add_node("tool_router", nodes.tool_router)
    builder.add_node("tool_executor", nodes.tool_executor)
    builder.add_node("evidence_normalizer", nodes.evidence_normalizer)
    builder.add_node("hypothesis_updater", nodes.hypothesis_updater)
    builder.add_node("stop_guard", nodes.stop_guard)
    builder.add_node("evidence_package_builder", nodes.evidence_package_builder)

    builder.add_edge(START, "resolve_hosts")
    builder.add_conditional_edges(
        "resolve_hosts",
        route_after_host_resolution,
        {
            "establish_phenomenon": "establish_phenomenon",
            "evidence_package_builder": "evidence_package_builder",
        },
    )
    builder.add_edge("establish_phenomenon", "coverage_sweep")
    builder.add_conditional_edges(
        "coverage_sweep",
        route_after_coverage_sweep,
        {
            "hypothesis_planner": "hypothesis_planner",
            "evidence_package_builder": "evidence_package_builder",
        },
    )
    builder.add_edge("hypothesis_planner", "observation_planner")
    builder.add_edge("observation_planner", "tool_router")
    builder.add_conditional_edges(
        "tool_router",
        route_after_tool_router,
        {
            "tool_executor": "tool_executor",
            "evidence_package_builder": "evidence_package_builder",
        },
    )
    builder.add_edge("tool_executor", "evidence_normalizer")
    builder.add_edge("evidence_normalizer", "hypothesis_updater")
    builder.add_edge("hypothesis_updater", "stop_guard")
    builder.add_conditional_edges(
        "stop_guard",
        route_after_stop_guard,
        {
            "observation_planner": "observation_planner",
            "coverage_sweep": "coverage_sweep",
            "evidence_package_builder": "evidence_package_builder",
        },
    )
    builder.add_edge("evidence_package_builder", END)
    return builder.compile(checkpointer=checkpointer)
