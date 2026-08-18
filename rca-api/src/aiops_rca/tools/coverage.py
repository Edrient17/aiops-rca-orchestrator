"""What the investigation has actually observed, and how to obtain the rest.

A report section declares the effects it is written from. This module answers
the two questions that declaration creates: which of those effects the
investigation has already covered, and how to collect the ones it has not
without asking a model to decide.

Coverage is deliberately about *looking*, not about finding. An event query
that returns nothing covers ``incident_events``: the window was searched and
the absence is the answer. Only an error leaves the effect uncovered, because
then nobody looked.
"""

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from aiops_rca.schemas.evidence_package import Evidence
from aiops_rca.schemas.investigation import PlannedToolCall, ResolvedHost
from aiops_rca.tools.registry import ToolPolicyError, ToolRegistry
from aiops_rca.tools.result import ToolExecutionResult

# Enough series to characterise a host without turning one sweep into the whole
# budget. get_metric_summary accepts twenty; a report reads a handful.
METRIC_ITEMS_PER_HOST = 8
DEFAULT_AGGREGATION = "1h"


def covered_effects(
    results: Iterable[ToolExecutionResult],
    evidence: Iterable[Evidence],
    registry: ToolRegistry,
) -> set[str]:
    """Effects some successful call has already produced evidence for."""

    produced = {item.tool_call_id for item in evidence}
    covered: set[str] = set()
    for result in results:
        if result.status == "error" or result.tool_call_id not in produced:
            continue
        try:
            policy = registry.get(result.tool_name)
        except ToolPolicyError:
            continue
        covered.update(policy.effects)
    return covered


@dataclass
class SweepContext:
    hosts: Sequence[ResolvedHost]
    window: Mapping[str, str]
    collection: Mapping[str, Any]
    execute: Callable[[str, dict[str, Any], str], Awaitable[ToolExecutionResult]]
    remaining: Callable[[], int]

    def aggregation(self) -> str:
        value = self.collection.get("aggregation")
        return value if isinstance(value, str) and value else DEFAULT_AGGREGATION

    def keywords(self) -> list[str]:
        raw = self.collection.get("metric_keywords")
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw if isinstance(item, str)][:20]


@dataclass
class SweepCall:
    result: ToolExecutionResult
    planned: PlannedToolCall
    host: ResolvedHost


class CoverageRecipe(Protocol):
    effects: tuple[str, ...]

    async def collect(self, context: SweepContext) -> list[SweepCall]: ...


def _call(
    result: ToolExecutionResult,
    tool_name: str,
    arguments: dict[str, Any],
    purpose: str,
    host: ResolvedHost,
) -> SweepCall:
    return SweepCall(
        result=result,
        planned=PlannedToolCall(
            tool_name=tool_name,
            arguments=arguments,
            purpose=purpose,
            target_hypothesis_ids=[],
            host_id=host.host_id,
        ),
        host=host,
    )


class EventRecipe:
    effects = ("incident_events", "trigger_anchor")

    async def collect(self, context: SweepContext) -> list[SweepCall]:
        calls: list[SweepCall] = []
        for host in context.hosts:
            if context.remaining() <= 0:
                break
            arguments = {
                "host_id": host.host_id,
                "time_from": context.window["from"],
                "time_to": context.window["to"],
            }
            result = await context.execute(
                "get_incident_events", arguments, host.host_id
            )
            calls.append(
                _call(
                    result,
                    "get_incident_events",
                    arguments,
                    f"Collect the declared incident events for {host.host}",
                    host,
                ),
            )
        return calls


class MetricRecipe:
    effects = ("metric_candidates", "metric_level", "metric_change", "metric_trend")

    async def collect(self, context: SweepContext) -> list[SweepCall]:
        keywords = context.keywords()
        if not keywords:
            return []
        calls: list[SweepCall] = []
        for host in context.hosts:
            # The pair is one observation. Starting it without room to finish
            # spends a call on a catalog nobody will read.
            if context.remaining() <= 1:
                break
            listing_arguments = {
                "host_id": host.host_id,
                "keywords": keywords,
                "limit": METRIC_ITEMS_PER_HOST,
            }
            listing = await context.execute(
                "list_relevant_metrics", listing_arguments, host.host_id
            )
            calls.append(
                _call(
                    listing,
                    "list_relevant_metrics",
                    listing_arguments,
                    f"Find the metrics the report declares for {host.host}",
                    host,
                ),
            )
            item_ids = _item_ids(listing.response)
            if not item_ids:
                continue
            summary_arguments = {
                "host_id": host.host_id,
                "item_ids": item_ids,
                "time_from": context.window["from"],
                "time_to": context.window["to"],
                "aggregation": context.aggregation(),
            }
            summary = await context.execute(
                "get_metric_summary", summary_arguments, host.host_id
            )
            calls.append(
                _call(
                    summary,
                    "get_metric_summary",
                    summary_arguments,
                    f"Measure the declared metrics over the window for {host.host}",
                    host,
                ),
            )
        return calls


class AuditRecipe:
    effects = ("audit_actor", "audit_command")

    async def collect(self, context: SweepContext) -> list[SweepCall]:
        calls: list[SweepCall] = []
        for host in context.hosts:
            if context.remaining() <= 0:
                break
            arguments = {
                "time_from": context.window["from"],
                "time_to": context.window["to"],
                "agent_name": host.host,
            }
            result = await context.execute(
                "get_wazuh_alert_summary", arguments, host.host_id
            )
            calls.append(
                _call(
                    result,
                    "get_wazuh_alert_summary",
                    arguments,
                    f"Collect the declared audit trail for {host.host}",
                    host,
                ),
            )
        return calls


DEFAULT_RECIPES: tuple[CoverageRecipe, ...] = (
    EventRecipe(),
    MetricRecipe(),
    AuditRecipe(),
)


def obtainable_effects(
    recipes: Iterable[CoverageRecipe] = DEFAULT_RECIPES,
) -> tuple[str, ...]:
    """Effects a sweep can collect without a model deciding to ask for them."""

    return tuple(sorted({effect for recipe in recipes for effect in recipe.effects}))


def recipes_for(
    wanted: Iterable[str],
    recipes: Iterable[CoverageRecipe] = DEFAULT_RECIPES,
) -> list[CoverageRecipe]:
    targets = set(wanted)
    return [recipe for recipe in recipes if targets.intersection(recipe.effects)]


def _item_ids(response: Any) -> list[str]:
    if not isinstance(response, Mapping):
        return []
    metrics = response.get("metrics")
    if not isinstance(metrics, list):
        return []
    ids = [
        str(item["item_id"])
        for item in metrics
        if isinstance(item, Mapping) and str(item.get("item_id") or "").isdigit()
    ]
    return ids[:METRIC_ITEMS_PER_HOST]
