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
# The Wazuh tools answer in PID order, and the low PIDs are kernel threads --
# a host with sixty services carries a hundred-odd of them. Fifty rows returned
# forty-nine threads and one service, so the number has to clear the threads
# before the interesting processes begin. Kernel threads are dropped from the
# evidence afterwards; this is only about asking for enough rows to reach past
# them.
AGENT_STATE_ROWS = 500


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
    #: The effects still missing. A recipe covers several, and running all of
    #: them regardless meant a template that declared only processes still paid
    #: for a port query on every host.
    wanted: frozenset[str] = frozenset()

    def asked_for(self, *effects: str) -> bool:
        # Empty means the caller did not say, which only happens in tests
        # predating this field; collecting everything is the old behaviour.
        return not self.wanted or bool(self.wanted.intersection(effects))

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


def service_processes(response: Any) -> list[Mapping[str, Any]]:
    """Processes worth reporting: everything that is not a kernel thread.

    A host runs a few dozen services and a hundred-odd kernel threads, and the
    threads hold the low PIDs. Asked what was running, a limit of fifty
    returned forty-nine kernel threads and nothing else -- the answer was past
    the cut, every time.
    """
    if not isinstance(response, Mapping):
        return []
    return [
        item
        for item in response.get("processes") or []
        if isinstance(item, Mapping) and not item.get("kernel_thread")
    ]


def _without_kernel_threads(result: ToolExecutionResult) -> ToolExecutionResult:
    """Drop kernel threads before the response becomes evidence.

    Evidence summaries are capped, so whatever is at the front of the list is
    all a report ever sees. Kernel threads hold the low PIDs and outnumber the
    services several times over, which is how a host running java and dockerd
    was reported as mm_percpu_wq and kswapd0.

    They are counted rather than silently discarded: the count is what makes
    the smaller list explainable.
    """
    services = service_processes(result.response)
    if not isinstance(result.response, Mapping):
        return result
    total = len(result.response.get("processes") or [])
    return result.model_copy(
        update={
            "response": {
                **result.response,
                "processes": services,
                "kernel_threads_omitted": total - len(services),
            },
        },
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


@dataclass(frozen=True)
class AgentRead:
    """One thing a resolved agent id can be exchanged for."""

    tool_name: str
    #: Built from the agent id, which is only known once the lookup has run.
    arguments: Callable[[str], dict[str, Any]]
    #: Applied before the result becomes evidence. Identity for most reads.
    shape: Callable[[ToolExecutionResult], ToolExecutionResult] = lambda result: result


class AgentStateRecipe:
    """What a host is running and listening on, right now.

    Two steps, because the Wazuh tools key on an agent id and an investigation
    knows a Zabbix host. get_wazuh_agents resolves one from the other by name,
    which is the same name Zabbix and Wazuh already agree on -- the alert tool
    filters by it too.

    The reads are a table rather than a chain of ifs. Written out, each effect
    named itself three times in this one function -- in `effects`, in the budget
    arithmetic, and in its own branch -- and the copy in the arithmetic was a
    copy, so a third read added without it would have miscounted the budget and
    gone wrong quietly. Adding one is now a single entry.
    """

    READS: Mapping[str, AgentRead] = {
        "current_process_state": AgentRead(
            "get_wazuh_agent_processes",
            lambda agent_id: {"agent_id": agent_id, "limit": AGENT_STATE_ROWS},
            _without_kernel_threads,
        ),
        "current_port_state": AgentRead(
            "get_wazuh_agent_ports",
            lambda agent_id: {
                "agent_id": agent_id,
                # The tool's enum: upper case for the state, lower for the
                # protocol. Either spelled the other way is refused before the
                # call reaches Wazuh.
                "protocol": "tcp",
                "state": "LISTENING",
                "limit": AGENT_STATE_ROWS,
            },
        ),
    }
    effects = tuple(READS)

    async def collect(self, context: SweepContext) -> list[SweepCall]:
        wanted = [
            read
            for effect, read in self.READS.items()
            if context.asked_for(effect)
        ]
        if not wanted:
            return []
        # The lookup plus the reads that were asked for. Starting without room
        # to finish spends a call on an id nothing will use.
        needed = 1 + len(wanted)

        calls: list[SweepCall] = []
        for host in context.hosts:
            if context.remaining() < needed:
                break
            lookup_arguments = {"name": host.host}
            lookup = await context.execute(
                "get_wazuh_agents", lookup_arguments, host.host_id
            )
            calls.append(
                _call(
                    lookup,
                    "get_wazuh_agents",
                    lookup_arguments,
                    f"Find the Wazuh agent reporting for {host.host}",
                    host,
                ),
            )
            agent_id = _agent_id(lookup.response, host.host)
            if agent_id is None:
                continue
            for read in wanted:
                arguments = read.arguments(agent_id)
                result = read.shape(
                    await context.execute(read.tool_name, arguments, host.host_id),
                )
                calls.append(
                    _call(
                        result,
                        read.tool_name,
                        arguments,
                        f"Read the current state of {host.host}",
                        host,
                    ),
                )
        return calls


DEFAULT_RECIPES: tuple[CoverageRecipe, ...] = (
    EventRecipe(),
    MetricRecipe(),
    AuditRecipe(),
    AgentStateRecipe(),
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


def _agent_id(response: Any, host: str) -> str | None:
    """The Wazuh agent id reporting for a host, or None.

    Returns None rather than guessing when the host does not appear: an id
    belonging to a different machine would read the wrong host's processes and
    look entirely plausible doing it.
    """
    if not isinstance(response, Mapping):
        return None
    for agent in response.get("agents") or []:
        if isinstance(agent, Mapping) and agent.get("name") == host:
            agent_id = agent.get("id")
            return str(agent_id) if agent_id is not None else None
    return None

