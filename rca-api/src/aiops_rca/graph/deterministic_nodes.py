"""Nodes that decide by rule rather than by asking a model.

One of them asks: `resolve_hosts` plans its own lookups, because which source
can name a given machine is a judgement and not a rule. Everything else here --
routing, execution, normalisation, the stop guard -- is policy that a rule can
settle, and settling it in code is what keeps it from drifting.
"""

import json
from collections.abc import Mapping
from importlib.resources import files
from typing import Any

from aiops_rca.graph.routing import hard_stop_update
from aiops_rca.graph.state import InvestigationState
from aiops_rca.schemas.investigation import PlannedToolCall, ResolvedHost, UnknownItem
from aiops_rca.services.model_contracts import host_search_decision_for
from aiops_rca.tools.executor import ToolExecutor
from aiops_rca.tools.normalizer import merge_evidence, normalize_observation
from aiops_rca.tools.registry import (
    DEFAULT_TOOL_REGISTRY,
    RoutingContext,
    ToolPolicyError,
    ToolRegistry,
)

#: Lookups the search gets before it gives up, spent only on names the fast
#: path could not resolve. Each is a model call and a tool call, and one more
#: model call follows the last of them: a tool whose answer nobody reads is a
#: call paid for and thrown away, which is what happened when the host was in
#: Wazuh and the first lookup had gone to the log index.
MAX_HOST_SEARCH_TURNS = 2
#: How many times a refused plan may be sent back to the planner before the
#: investigation gives up on it. Each retry costs an iteration, so the budget
#: bounds it too; this bounds it tightly enough that a planner stuck on one
#: malformed candidate does not spend the whole investigation on it.
MAX_ROUTING_ATTEMPTS = 2


class ResolveHostsNode:
    """Find the hosts this investigation is about, wherever they are named.

    This called `find_hosts` and nothing else, so a machine outside Zabbix could
    not be investigated at all -- and the same name sits in a log index and in a
    Wazuh agent list. Keeping Zabbix as a fast path kept one tool's name in the
    pipeline for the sake of a model call, which is the trade this project has
    decided against.

    It plans its lookups now. A turn names a tool and arguments, the registry
    validates the call, and the next turn reads what came back. The host
    selector the template supplies is passed through as data: this node does not
    know what a host group is, only that the report asked for one.

    A name is left unresolved rather than guessed at. Where a request names one
    host and the search reports several, none is chosen -- picking would produce
    an investigation of the wrong machine that reads exactly like the right one.
    """

    def __init__(
        self,
        *,
        model: Any,
        model_name: str,
        executor: Any,
        registry: ToolRegistry = DEFAULT_TOOL_REGISTRY,
    ) -> None:
        self.model = model
        self.model_name = model_name
        self.executor = executor
        self.registry = registry

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        results = list(state.tool_results)
        errors = list(state.tool_errors)
        unknowns = list(state.unknowns)
        purposes = dict(state.tool_call_purposes)

        wanted = list(state.parsed_request.host_queries)
        selector = (state.collection or {}).get("host_selector") or {
            "mode": "from_question"
        }

        found, search_unknowns, produced = await self._search(
            state, wanted, selector, results,
        )
        for result in produced:
            results.append(result)
            purposes[result.tool_call_id] = "Resolve the requested investigation hosts"
            if result.status == "error":
                errors.append(result)
        unknowns.extend(search_unknowns)

        resolved: dict[str, ResolvedHost] = {}
        unresolved: list[str] = []
        for query in wanted:
            matches = [host for host in found if host.query == query]
            if len(matches) == 1:
                resolved.setdefault(matches[0].host, matches[0])
            elif matches:
                unresolved.append(query)
                unknowns.append(
                    UnknownItem(
                        code="host_ambiguous",
                        message=(
                            f"Several hosts matched {query!r}: "
                            + ", ".join(sorted(host.host for host in matches))
                            + ". None was selected automatically."
                        ),
                        host_query=query,
                    ),
                )
            else:
                unresolved.append(query)
                unknowns.append(
                    UnknownItem(
                        code="host_not_found",
                        message=f"No host matched {query!r} in any source searched.",
                        host_query=query,
                    ),
                )

        # A selector that names hosts itself -- a host group, say -- asks for
        # whatever the lookup returned rather than for one name each.
        for host in found:
            if host.query is None:
                resolved.setdefault(host.host, host)

        stop_reason = state.stop_reason
        if not resolved:
            stop_reason = "no host could be resolved for investigation"
        return {
            "hosts": list(resolved.values()),
            "unresolved_hosts": _dedupe(unresolved),
            "unknowns": unknowns,
            "tool_results": results,
            "tool_errors": errors,
            "tool_call_count": len(results),
            "tool_call_purposes": purposes,
            "last_observation": results[-1] if results else state.last_observation,
            "stop_reason": stop_reason,
            "visited_nodes": [*state.visited_nodes, "resolve_hosts"],
        }

    async def _search(
        self,
        state: InvestigationState,
        wanted: list[str],
        selector: Mapping[str, Any],
        results: list[Any],
    ) -> tuple[list[ResolvedHost], list[UnknownItem], list[Any]]:
        """Look for these hosts, in whichever sources can name them."""
        found: list[ResolvedHost] = []
        unknowns: list[UnknownItem] = []
        produced: list[Any] = []
        output_type = host_search_decision_for(self.registry.names())
        seen: list[dict[str, Any]] = []

        for turn in range(MAX_HOST_SEARCH_TURNS + 1):
            # The extra turn reads the last lookup rather than making another.
            # A tool whose answer nobody reads is a call paid for and thrown
            # away, which is how a host that was in Wazuh came back missing.
            reading_only = turn == MAX_HOST_SEARCH_TURNS
            if reading_only and not seen:
                break
            if not reading_only and (
                len(results) + len(produced) >= state.limits.max_tool_calls
            ):
                unknowns.append(
                    UnknownItem(
                        code="host_search_budget_exhausted",
                        message=(
                            "The tool-call budget was exhausted before "
                            + (", ".join(wanted) or "the requested hosts")
                            + " could be looked for."
                        ),
                    ),
                )
                break

            decision = await self.model.complete(
                model=self.model_name,
                output_type=output_type,
                system_prompt=_prompt("host_search.md"),
                payload={
                    "unresolved": wanted,
                    "host_selector": selector,
                    "attempts": seen,
                    "tool_catalog": state.tool_catalog,
                },
                reasoning_effort="low",
            )
            for item in decision.hosts:
                if any(item.host == existing.host for existing in found):
                    continue
                found.append(
                    ResolvedHost(
                        host=item.host,
                        host_id=item.host_id,
                        query=_matching_query(item.host, wanted),
                        found_by=item.found_by,
                    ),
                )
            if reading_only or not decision.tool_name:
                break

            try:
                arguments = json.loads(decision.arguments_json)
                if not isinstance(arguments, dict):
                    raise ValueError("arguments_json must decode to an object")
            except ValueError as error:
                unknowns.append(
                    UnknownItem(
                        code="host_search_unusable",
                        message=f"{decision.tool_name}: {error}",
                    ),
                )
                break

            planned = PlannedToolCall(
                tool_name=decision.tool_name,
                arguments=arguments,
                purpose="Resolve the requested investigation hosts",
                target_hypothesis_ids=[],
            )
            try:
                result = await self.executor.execute(
                    planned,
                    RoutingContext(
                        tool_call_count=len(results) + len(produced),
                        max_tool_calls=state.limits.max_tool_calls,
                        # Finding a name means reading whatever index or list
                        # holds it, and those are the generic tools.
                        generic_fallback_allowed=True,
                    ),
                )
            except ToolPolicyError as error:
                unknowns.append(
                    UnknownItem(
                        code="host_search_blocked",
                        message=f"{decision.tool_name}: {error}",
                    ),
                )
                break

            produced.append(result)
            seen.append(
                {
                    "tool_name": decision.tool_name,
                    "arguments": arguments,
                    "status": result.status,
                    "response": _bounded_response(result),
                },
            )
            if result.status == "error":
                unknowns.append(
                    UnknownItem(
                        code="host_search_error",
                        message=result.error or f"{decision.tool_name} failed",
                        tool_call_id=result.tool_call_id,
                    ),
                )

        return found, unknowns, produced


class ToolRouterNode:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        question = state.next_question
        if not question or not question.required_tool:
            return {
                "stop_reason": "no routable observation question remains",
                "planned_tool_call": None,
                "visited_nodes": [*state.visited_nodes, "tool_router"],
            }
        context = RoutingContext(
            temporal_scope=question.temporal_scope,
            generic_fallback_allowed=state.generic_fallback_allowed,
            tool_call_count=state.tool_call_count,
            max_tool_calls=state.limits.max_tool_calls,
        )
        try:
            policy = self.registry.validate_call(
                question.required_tool,
                state.candidate_tool_arguments.get(question.required_tool) or {},
                context,
                _catalog_scope(state.tool_catalog, question.required_tool),
            )
        except ToolPolicyError as error:
            # The registry already says which of three things went wrong; a
            # prefix asserting a missing tool would put back the very sentence
            # that made an unproposed call read as a capability the platform
            # lacks.
            unknowns = [
                *state.unknowns,
                UnknownItem(code="tool_routing_blocked", message=str(error)),
            ]
            attempts = state.routing_attempts + 1
            if error.retryable and attempts <= MAX_ROUTING_ATTEMPTS:
                # A refused plan is a plan to redo, not the end of the
                # investigation. The report writer has always been allowed a
                # second draft against the reason its first was sent back; a
                # planner that named a source where a host belonged got no such
                # turn, and one malformed candidate closed the whole run.
                return {
                    "planned_tool_call": None,
                    "next_question": None,
                    "unknowns": unknowns,
                    "routing_rejections": [*state.routing_rejections, str(error)],
                    "routing_attempts": attempts,
                    "visited_nodes": [*state.visited_nodes, "tool_router"],
                }
            return {
                "stop_reason": f"the next observation could not be routed: {error}",
                "planned_tool_call": None,
                "unknowns": unknowns,
                "routing_rejections": [*state.routing_rejections, str(error)],
                "routing_attempts": attempts,
                "visited_nodes": [*state.visited_nodes, "tool_router"],
            }
        # The candidate names a host; its Zabbix id comes from what was
        # resolved, and may not exist. A tool that needs one says so in its own
        # required arguments, so nothing here has to know which tools those are.
        host = state.candidate_tool_hosts.get(policy.name)
        host_id = next(
            (item.host_id for item in state.hosts if item.host == host), None
        )
        return {
            "routing_rejections": [],
            "routing_attempts": 0,
            "planned_tool_call": PlannedToolCall(
                tool_name=policy.name,
                arguments=dict(state.candidate_tool_arguments[policy.name]),
                purpose=question.question,
                target_hypothesis_ids=question.discriminates_hypothesis_ids,
                host=host,
                host_id=host_id,
            ),
            "visited_nodes": [*state.visited_nodes, "tool_router"],
        }


class ToolExecutorNode:
    def __init__(self, executor: ToolExecutor) -> None:
        self.executor = executor

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        planned = state.planned_tool_call
        if not planned:
            return {
                "fatal_error": "tool_executor entered without planned_tool_call",
                "visited_nodes": [*state.visited_nodes, "tool_executor"],
            }
        temporal_scope = (
            state.next_question.temporal_scope if state.next_question else "timeless"
        )
        result = await self.executor.execute(
            planned,
            RoutingContext(
                temporal_scope=temporal_scope,
                generic_fallback_allowed=state.generic_fallback_allowed,
                tool_call_count=state.tool_call_count,
                max_tool_calls=state.limits.max_tool_calls,
            ),
        )
        results = [*state.tool_results, result]
        errors = (
            [*state.tool_errors, result]
            if result.status == "error"
            else state.tool_errors
        )
        unknowns = list(state.unknowns)
        if result.status == "error":
            unknowns.append(
                UnknownItem(
                    code="tool_error",
                    message=result.error or f"{result.tool_name} failed",
                    tool_call_id=result.tool_call_id,
                ),
            )
        if result.status == "partial":
            # The reply stopped at a limit rather than at the end of the data,
            # so its count is a floor. Nothing downstream can tell that from a
            # complete answer once the rows are normalized into evidence, and a
            # report that reads it as the total understates the month.
            unknowns.append(
                UnknownItem(
                    code="result_truncated",
                    message=(
                        f"{result.tool_name} reached its result limit, so the"
                        " returned count is a lower bound and the window is only"
                        " partly covered"
                    ),
                    tool_call_id=result.tool_call_id,
                ),
            )
        return {
            "last_observation": result,
            "tool_results": results,
            "tool_errors": errors,
            "unknowns": unknowns,
            "tool_call_count": len(results),
            "tool_call_purposes": {
                **state.tool_call_purposes,
                result.tool_call_id: planned.purpose,
            },
            "visited_nodes": [*state.visited_nodes, "tool_executor"],
        }


class EvidenceNormalizerNode:
    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        if not state.last_observation or not state.planned_tool_call:
            return {
                "fatal_error": "evidence_normalizer entered without an observation and plan",
                "visited_nodes": [*state.visited_nodes, "evidence_normalizer"],
            }
        host = _host_for_plan(state)
        if not host:
            return {
                "fatal_error": "no resolved host could be associated with the observation",
                "visited_nodes": [*state.visited_nodes, "evidence_normalizer"],
            }
        additions = normalize_observation(
            state.last_observation,
            state.planned_tool_call,
            host_id=host.host_id,
            host=host.host,
        )
        evidence, merge_unknowns = merge_evidence(state.evidence, additions)
        return {
            "evidence": evidence,
            "unknowns": [*state.unknowns, *merge_unknowns],
            "visited_nodes": [*state.visited_nodes, "evidence_normalizer"],
        }


class StopGuardNode:
    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        return {
            **hard_stop_update(state),
            "visited_nodes": [*state.visited_nodes, "stop_guard"],
        }


def _host_for_plan(state: InvestigationState) -> ResolvedHost | None:
    arguments = state.planned_tool_call.arguments if state.planned_tool_call else {}
    planned_host = state.planned_tool_call.host if state.planned_tool_call else None
    wanted_id = str(arguments.get("host_id") or "")
    wanted_name = str(arguments.get("host") or arguments.get("agent_name") or "")
    for host in state.hosts:
        # The name first: it is the identity, and the id may not exist.
        if planned_host and host.host == planned_host:
            return host
        if wanted_id and host.host_id == wanted_id:
            return host
        if wanted_id and host.host_id == wanted_id:
            return host
        if wanted_name and host.host.casefold() == wanted_name.casefold():
            return host
    return state.hosts[0] if len(state.hosts) == 1 else None


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _catalog_scope(catalog: list[dict[str, Any]], name: str) -> str:
    """What the live catalog says about when this tool's answer is true.

    Read from the catalog rather than from the registry, so the fact belongs to
    the server that owns the tool. A tool the catalog does not describe gets the
    default, which is also what every tool got before any server declared.
    """
    for item in catalog:
        if item.get("name") == name:
            scope = item.get("temporal_scope")
            return scope if isinstance(scope, str) else "any"
    return "any"


def _prompt(name: str) -> str:
    return (files("aiops_rca.prompts") / name).read_text(encoding="utf-8")


def _matching_query(host: str, unresolved: list[str]) -> str | None:
    """Which requested name this host answers, when one of them plainly does."""
    folded = host.casefold()
    for query in unresolved:
        if query.casefold() in folded or folded in query.casefold():
            return query
    return None


def _bounded_response(result: Any) -> Any:
    """Enough of the response for the model to read names out of it."""
    text = json.dumps(result.response, ensure_ascii=False, default=str)
    return text[:8000]
