"""Reusable non-LLM nodes for the first collector graph implementation."""

from collections.abc import Mapping
from typing import Any

from aiops_rca.graph.routing import hard_stop_update
from aiops_rca.graph.state import InvestigationState
from aiops_rca.schemas.investigation import PlannedToolCall, ResolvedHost, UnknownItem
from aiops_rca.tools.adapters.base import McpAdapter
from aiops_rca.tools.executor import ToolExecutor
from aiops_rca.tools.normalizer import merge_evidence, normalize_observation
from aiops_rca.tools.registry import (
    RoutingContext,
    ToolPolicyError,
    ToolRegistry,
)


class ResolveHostsNode:
    def __init__(self, zabbix: McpAdapter) -> None:
        self.zabbix = zabbix

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        selector = (state.collection or {}).get("host_selector") or {
            "mode": "from_question"
        }
        mode = selector.get("mode", "from_question")
        if mode == "host_group":
            requests = [{"group_ids": selector.get("group_ids") or []}]
        else:
            requests = [{"query": query} for query in state.parsed_request.host_queries]

        results = list(state.tool_results)
        errors = list(state.tool_errors)
        resolved = {host.host_id: host for host in state.hosts}
        unresolved = list(state.unresolved_hosts)
        unknowns = list(state.unknowns)
        purposes = dict(state.tool_call_purposes)

        for index, arguments in enumerate(requests):
            if len(results) >= state.limits.max_tool_calls:
                for skipped in requests[index:]:
                    if query := skipped.get("query"):
                        unresolved.append(str(query))
                unknowns.append(
                    UnknownItem(
                        code="host_resolution_budget_exhausted",
                        message="The tool-call budget was exhausted before every host expression could be resolved.",
                    ),
                )
                break
            context = RoutingContext(
                tool_call_count=len(results),
                max_tool_calls=state.limits.max_tool_calls,
            )
            result = await self.zabbix.execute("find_hosts", arguments, context)
            results.append(result)
            purposes[result.tool_call_id] = "Resolve the requested investigation hosts"
            if result.status == "error":
                errors.append(result)
                query = arguments.get("query")
                if query:
                    unresolved.append(str(query))
                unknowns.append(
                    UnknownItem(
                        code="host_resolution_error",
                        message=result.error or "find_hosts failed",
                        host_query=str(query) if query else None,
                        tool_call_id=result.tool_call_id,
                    ),
                )
                continue

            response = result.response if isinstance(result.response, Mapping) else {}
            candidates = response.get("hosts")
            candidates = candidates if isinstance(candidates, list) else []
            if mode == "host_group":
                for candidate in candidates:
                    host = _resolved_host(candidate)
                    if host:
                        resolved[host.host_id] = host
                if response.get("excluded_group_ids"):
                    unknowns.append(
                        UnknownItem(
                            code="host_groups_excluded",
                            message=f"Host groups were excluded: {response['excluded_group_ids']}",
                            tool_call_id=result.tool_call_id,
                        ),
                    )
                if response.get("truncated") is True:
                    unknowns.append(
                        UnknownItem(
                            code="host_group_truncated",
                            message="Host-group resolution was truncated.",
                            tool_call_id=result.tool_call_id,
                        ),
                    )
                continue

            query = str(arguments["query"])
            selected = _select_exact_or_unambiguous(query, candidates)
            if selected:
                host = _resolved_host(selected, query=query)
                if host:
                    resolved[host.host_id] = host
            else:
                unresolved.append(query)
                code = "host_not_found" if not candidates else "host_ambiguous"
                message = (
                    f"No Zabbix host matched {query!r}."
                    if not candidates
                    else f"Several Zabbix hosts matched {query!r}; none was selected automatically."
                )
                unknowns.append(
                    UnknownItem(
                        code=code,
                        message=message,
                        host_query=query,
                        tool_call_id=result.tool_call_id,
                    ),
                )

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
            )
        except ToolPolicyError as error:
            return {
                # The registry already says which of three things went wrong;
                # a prefix asserting a missing tool would put back the very
                # sentence that made an unproposed call read as a capability
                # the platform lacks.
                "stop_reason": f"the next observation could not be routed: {error}",
                "planned_tool_call": None,
                "unknowns": [
                    *state.unknowns,
                    UnknownItem(code="tool_routing_blocked", message=str(error)),
                ],
                "visited_nodes": [*state.visited_nodes, "tool_router"],
            }
        return {
            "planned_tool_call": PlannedToolCall(
                tool_name=policy.name,
                arguments=dict(state.candidate_tool_arguments[policy.name]),
                purpose=question.question,
                target_hypothesis_ids=question.discriminates_hypothesis_ids,
                host_id=state.candidate_tool_hosts.get(policy.name),
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


def _select_exact_or_unambiguous(
    query: str, candidates: list[Any]
) -> Mapping[str, Any] | None:
    mappings = [candidate for candidate in candidates if isinstance(candidate, Mapping)]
    lowered = query.casefold()
    exact = [
        candidate
        for candidate in mappings
        if str(candidate.get("host", "")).casefold() == lowered
        or str(candidate.get("name", "")).casefold() == lowered
    ]
    if len(exact) == 1:
        return exact[0]
    if len(mappings) == 1:
        return mappings[0]
    return None


def _resolved_host(candidate: Any, *, query: str | None = None) -> ResolvedHost | None:
    if not isinstance(candidate, Mapping):
        return None
    host_id = str(candidate.get("host_id") or "")
    host = str(candidate.get("host") or candidate.get("name") or "")
    if not host_id.isdigit() or not host:
        return None
    return ResolvedHost(host=host, host_id=host_id, query=query)


def _host_for_plan(state: InvestigationState) -> ResolvedHost | None:
    arguments = state.planned_tool_call.arguments if state.planned_tool_call else {}
    planned_host_id = (
        state.planned_tool_call.host_id if state.planned_tool_call else None
    )
    wanted_id = str(arguments.get("host_id") or "")
    wanted_name = str(arguments.get("host") or arguments.get("agent_name") or "")
    for host in state.hosts:
        if planned_host_id and host.host_id == planned_host_id:
            return host
        if wanted_id and host.host_id == wanted_id:
            return host
        if wanted_name and host.host.casefold() == wanted_name.casefold():
            return host
    return state.hosts[0] if len(state.hosts) == 1 else None


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
