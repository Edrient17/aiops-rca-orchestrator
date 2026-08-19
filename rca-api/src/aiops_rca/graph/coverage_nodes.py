"""Collect what the report template declared, without a model deciding to.

The hypothesis loop stops when no further observation discriminates between
competing explanations. That is the right instinct for an incident and the
wrong one for a report whose sections are measurements: a monthly capacity
report once ended with its capacity sections empty because the investigation
found a real incident and reasoned about that instead.

This node closes the gap deterministically. It reads the effects the template's
sections declared, subtracts the ones already observed, and runs the recipe for
each of the rest. What it cannot collect it records, so an empty section always
has a stated reason.
"""

from collections.abc import Mapping
from typing import Any

from aiops_rca.graph.state import InvestigationState
from aiops_rca.schemas.investigation import PlannedToolCall, UnknownItem
from aiops_rca.tools.coverage import (
    DEFAULT_RECIPES,
    CoverageRecipe,
    SweepCall,
    SweepContext,
    covered_effects,
    recipes_for,
)
from aiops_rca.tools.executor import ToolExecutor
from aiops_rca.tools.normalizer import merge_evidence, normalize_observation
from aiops_rca.tools.registry import RoutingContext, ToolRegistry
from aiops_rca.tools.result import ToolExecutionResult


def declared_effects(state: InvestigationState) -> tuple[str, ...]:
    """Effects the selected template's sections say they are written from."""

    collection = state.collection or {}
    declared = collection.get("required_effects")
    if not isinstance(declared, list):
        return ()
    return tuple(str(item) for item in declared if isinstance(item, str))


def pending_effects(
    state: InvestigationState,
    registry: ToolRegistry,
) -> tuple[str, ...]:
    """Declared effects with no evidence yet that the sweep has not tried."""

    covered = covered_effects(state.tool_results, state.evidence, registry)
    attempted = set(state.swept_effects)
    return tuple(
        effect
        for effect in declared_effects(state)
        if effect not in covered and effect not in attempted
    )


class CoverageSweepNode:
    def __init__(
        self,
        executor: ToolExecutor,
        registry: ToolRegistry,
        recipes: tuple[CoverageRecipe, ...] = DEFAULT_RECIPES,
    ) -> None:
        self.executor = executor
        self.registry = registry
        self.recipes = recipes

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        pending = pending_effects(state, self.registry)
        if not pending or not state.hosts:
            return {"visited_nodes": [*state.visited_nodes, "coverage_sweep"]}

        window = _window(state)
        if window is None:
            return {
                "swept_effects": [*state.swept_effects, *pending],
                "unknowns": [
                    *state.unknowns,
                    UnknownItem(
                        code="coverage_window_missing",
                        message=(
                            "The template declared effects to collect but no"
                            " investigation window was resolved: "
                            + ", ".join(pending)
                        ),
                    ),
                ],
                "visited_nodes": [*state.visited_nodes, "coverage_sweep"],
            }

        results = list(state.tool_results)
        errors = list(state.tool_errors)
        unknowns = list(state.unknowns)
        evidence = list(state.evidence)
        purposes = dict(state.tool_call_purposes)

        async def execute(
            tool_name: str,
            arguments: dict[str, Any],
            host_id: str,
        ) -> ToolExecutionResult:
            planned = PlannedToolCall(
                tool_name=tool_name,
                arguments=arguments,
                purpose=f"declared coverage: {tool_name}",
                target_hypothesis_ids=[],
                host_id=host_id,
            )
            return await self.executor.execute(
                planned,
                RoutingContext(
                    tool_call_count=len(results),
                    max_tool_calls=state.limits.max_tool_calls,
                ),
            )

        context = SweepContext(
            hosts=state.hosts,
            window=window,
            collection=state.collection or {},
            execute=execute,
            remaining=lambda: state.limits.max_tool_calls - len(results),
            wanted=frozenset(pending),
        )

        for recipe in recipes_for(pending, self.recipes):
            for call in await recipe.collect(context):
                results.append(call.result)
                purposes[call.result.tool_call_id] = call.planned.purpose
                if call.result.status == "error":
                    errors.append(call.result)
                    unknowns.append(
                        UnknownItem(
                            code="coverage_collection_error",
                            message=(
                                call.result.error
                                or f"{call.result.tool_name} failed during the"
                                " declared coverage sweep"
                            ),
                            tool_call_id=call.result.tool_call_id,
                        ),
                    )
                    continue
                evidence, merge_unknowns = merge_evidence(evidence, _normalize(call))
                unknowns.extend(merge_unknowns)

        swept = [*state.swept_effects, *pending]
        still_missing = [
            effect
            for effect in pending
            if effect not in covered_effects(results, evidence, self.registry)
        ]
        if still_missing:
            # A section built from these will have nothing to cite. Saying so
            # here is what turns an inexplicably empty section into a stated
            # limitation.
            unknowns.append(
                UnknownItem(
                    code="declared_effect_uncovered",
                    message=(
                        "The report declares sections built from observations"
                        " this investigation could not collect: "
                        + ", ".join(sorted(still_missing))
                    ),
                ),
            )

        return {
            "evidence": evidence,
            "unknowns": unknowns,
            "tool_results": results,
            "tool_errors": errors,
            "tool_call_count": len(results),
            "tool_call_purposes": purposes,
            "swept_effects": swept,
            "visited_nodes": [*state.visited_nodes, "coverage_sweep"],
        }


def _normalize(call: SweepCall) -> list[Any]:
    return normalize_observation(
        call.result,
        call.planned,
        host_id=call.host.host_id,
        host=call.host.host,
    )


def _window(state: InvestigationState) -> dict[str, str] | None:
    collection = state.collection or {}
    window = collection.get("resolved_window")
    if (
        isinstance(window, Mapping)
        and isinstance(window.get("from"), str)
        and isinstance(window.get("to"), str)
    ):
        return {"from": window["from"], "to": window["to"]}
    return None
