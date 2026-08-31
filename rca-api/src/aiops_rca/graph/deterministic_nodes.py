"""Nodes that decide by rule rather than by asking a model.

One of them asks: `resolve_hosts` plans its own lookups, because which source
can name a given machine is a judgement and not a rule. Everything else here --
routing, execution, normalisation, the stop guard -- is policy that a rule can
settle, and settling it in code is what keeps it from drifting.
"""

import asyncio
import json
from collections.abc import Mapping
from importlib.resources import files
from typing import Any

from aiops_rca.graph.routing import hard_stop_update
from aiops_rca.graph.state import InvestigationState
from aiops_rca.schemas.investigation import PlannedToolCall, ResolvedHost, UnknownItem
from aiops_rca.services.model_contracts import host_search_decision_for
from aiops_rca.tools.adapters.base import describe_failure
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
#: Past this, an answer is not an observation any more, it is a haystack. One
#: log search came back at 175,834 characters -- 25,369 tokens in the single
#: model call that read it, more than half of that whole investigation -- and
#: the same day of the same host is 24 rows when asked as an aggregate.
#:
#: The threshold does not cut anything. It decides when to say the query was
#: shaped wrong, which is a fact the planner and the report both want.
MAX_REASONABLE_RESPONSE_CHARS = 40_000


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
            "last_observations": produced,
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
            # What is still missing, rather than what was asked for. The whole
            # request went back every turn, so a name reported as found on one
            # turn was presented as unresolved on the next and the model went
            # looking for it again -- three calls and 23,137 tokens to resolve
            # one host the question had named outright.
            still = [
                query
                for query in wanted
                if not any(host.query == query for host in found)
            ]
            # Nothing left to look for. `found` has to be non-empty as well:
            # a selector that names its own hosts -- a group -- asks for no
            # name at all, and stopping on that would be stopping before the
            # first lookup.
            if found and not still:
                break

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
                system_prompt=_prompt("host_search.md", "log_queries.md"),
                payload={
                    "tool_catalog": state.tool_catalog,
                    "unresolved": still,
                    "host_selector": selector,
                    "attempts": seen,
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
    """Validate this turn's questions and turn the survivors into calls.

    One question used to arrive with several candidate calls, of which this
    picked whichever matched `required_tool`. Now several questions arrive, each
    with the one call that answers it, and the work is the same for each: the
    registry judges it, and a host name becomes whatever id the tools ask for.

    A batch survives partial failure. Some questions route and some are refused,
    and only a turn where nothing survives goes back to the planner.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        if not state.next_questions:
            return {
                "stop_reason": "no routable observation question remains",
                "planned_tool_calls": [],
                "visited_nodes": [*state.visited_nodes, "tool_router"],
            }

        planned: list[PlannedToolCall] = []
        unknowns = list(state.unknowns)
        refusals: list[str] = []
        host_ids = {item.host: item.host_id for item in state.hosts}

        for question in state.next_questions:
            if not question.required_tool:
                continue
            context = RoutingContext(
                temporal_scope=question.temporal_scope,
                generic_fallback_allowed=question.generic_fallback_allowed,
                # Counted across the batch as well as the investigation: four
                # calls planned together still spend four of the budget.
                tool_call_count=state.tool_call_count + len(planned),
                max_tool_calls=state.limits.max_tool_calls,
            )
            try:
                policy = self.registry.validate_call(
                    question.required_tool,
                    question.arguments,
                    context,
                    _catalog_scope(state.tool_catalog, question.required_tool),
                )
            except ToolPolicyError as error:
                # The registry already says which of three things went wrong; a
                # prefix asserting a missing tool would put back the very
                # sentence that made an unproposed call read as a capability the
                # platform lacks.
                unknowns.append(
                    UnknownItem(code="tool_routing_blocked", message=str(error)),
                )
                if error.retryable:
                    refusals.append(str(error))
                continue
            planned.append(
                PlannedToolCall(
                    tool_name=policy.name,
                    arguments=dict(question.arguments),
                    purpose=question.question,
                    target_hypothesis_ids=question.discriminates_hypothesis_ids,
                    host=question.host,
                    # The call names a host; its Zabbix id comes from what was
                    # resolved, and may not exist. A tool that needs one says so
                    # in its own required arguments, so nothing here has to know
                    # which tools those are.
                    host_id=host_ids.get(question.host or ""),
                ),
            )

        if planned:
            # A rejection the planner has already worked around is noise in the
            # next payload; the permanent record of it is in unknowns.
            return {
                "planned_tool_calls": planned,
                "routing_rejections": [],
                "routing_attempts": 0,
                "unknowns": unknowns,
                "visited_nodes": [*state.visited_nodes, "tool_router"],
            }

        attempts = state.routing_attempts + 1
        if refusals and attempts <= MAX_ROUTING_ATTEMPTS:
            # A refused plan is a plan to redo, not the end of the
            # investigation. The report writer has always been allowed a second
            # draft against the reason its first was sent back; a planner that
            # named a source where a host belonged got no such turn, and one
            # malformed candidate closed the whole run.
            return {
                "planned_tool_calls": [],
                "next_questions": [],
                "unknowns": unknowns,
                "routing_rejections": [*state.routing_rejections, *refusals],
                "routing_attempts": attempts,
                "visited_nodes": [*state.visited_nodes, "tool_router"],
            }
        return {
            "stop_reason": (
                "the next observation could not be routed: "
                + (refusals[0] if refusals else "no proposal was allowed")
            ),
            "planned_tool_calls": [],
            "unknowns": unknowns,
            "routing_rejections": [*state.routing_rejections, *refusals],
            "routing_attempts": attempts,
            "visited_nodes": [*state.visited_nodes, "tool_router"],
        }


class ToolExecutorNode:
    """Make this turn's calls, together.

    They were made one per cycle, each waiting on a planning turn it did not
    depend on. A tool call takes three tenths of a second and the model turns
    around it take twenty-six, so the waiting was the whole latency.
    """

    def __init__(self, executor: ToolExecutor) -> None:
        self.executor = executor

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        planned = list(state.planned_tool_calls)
        if not planned:
            return {
                "fatal_error": "tool_executor entered without a planned tool call",
                "visited_nodes": [*state.visited_nodes, "tool_executor"],
            }
        # Keyed by the call, not by the tool. Two questions of the same tool --
        # one about now and one about last night -- collapsed onto whichever
        # came last, so a batch was executed under a context the router never
        # granted it. The adapter validates a second time, so the disagreement
        # did not produce a wrong answer: it refused calls the router had
        # already allowed, and a turn where every call is refused reaches the
        # normalizer with nothing to normalize.
        #
        # The router writes the question into `purpose`, which is what makes
        # the pair identify the question that asked for this call. It is the
        # same pairing `_plan_for` uses on the way back.
        asked = {
            (question.required_tool, question.question): question
            for question in state.next_questions
            if question.required_tool
        }

        async def run(call: PlannedToolCall, offset: int) -> Any:
            question = asked.get((call.tool_name, call.purpose))
            return await self.executor.execute(
                call,
                RoutingContext(
                    temporal_scope=(
                        question.temporal_scope if question else "timeless"
                    ),
                    generic_fallback_allowed=(
                        question.generic_fallback_allowed if question else False
                    ),
                    tool_call_count=state.tool_call_count + offset,
                    max_tool_calls=state.limits.max_tool_calls,
                ),
            )

        produced = await asyncio.gather(
            *(run(call, offset) for offset, call in enumerate(planned)),
            return_exceptions=True,
        )

        results = list(state.tool_results)
        errors = list(state.tool_errors)
        unknowns = list(state.unknowns)
        purposes = dict(state.tool_call_purposes)
        observations: list[Any] = []

        for call, outcome in zip(planned, produced, strict=True):
            if isinstance(outcome, BaseException):
                # One call raising must not take the others down with it: they
                # already ran, and their answers are paid for.
                unknowns.append(
                    UnknownItem(
                        code="tool_call_failed",
                        message=f"{call.tool_name}: {describe_failure(outcome)}",
                    ),
                )
                continue
            observations.append(outcome)
            results.append(outcome)
            purposes[outcome.tool_call_id] = call.purpose
            if outcome.status == "error":
                errors.append(outcome)
                unknowns.append(
                    UnknownItem(
                        code="tool_error",
                        message=outcome.error or f"{outcome.tool_name} failed",
                        tool_call_id=outcome.tool_call_id,
                    ),
                )
            if outcome.status == "partial":
                # The reply stopped at a limit rather than at the end of the
                # data, so its count is a floor. Nothing downstream can tell
                # that from a complete answer once the rows are normalized into
                # evidence, and a report that reads it as the total understates
                # the month.
                unknowns.append(
                    UnknownItem(
                        code="result_truncated",
                        message=(
                            f"{outcome.tool_name} reached its result limit, so"
                            " the returned count is a lower bound and the window"
                            " is only partly covered"
                        ),
                        tool_call_id=outcome.tool_call_id,
                    ),
                )
            oversized = _response_chars(outcome)
            if oversized > MAX_REASONABLE_RESPONSE_CHARS:
                # Nothing is truncated here. The answer travels whole, because
                # cutting it would take away the very rows a hypothesis is
                # judged on. What is added is that the shape was wrong: a
                # question about how much or how often is answered by an
                # aggregate in a few rows, and this one came back as a wall of
                # documents.
                unknowns.append(
                    UnknownItem(
                        code="response_too_large_to_reason_over",
                        message=(
                            f"{outcome.tool_name} returned {oversized:,}"
                            " characters. A question about counts or timing is"
                            " answered by an aggregate in a few rows; fetch"
                            " documents only once an aggregate says which ones"
                            " to read."
                        ),
                        tool_call_id=outcome.tool_call_id,
                    ),
                )

        return {
            "tool_results": results,
            "tool_errors": errors,
            "unknowns": unknowns,
            "tool_call_purposes": purposes,
            "tool_call_count": len(results),
            "last_observations": observations,
            "visited_nodes": [*state.visited_nodes, "tool_executor"],
        }


class EvidenceNormalizerNode:
    """Turn this turn's answers into evidence, each against its own call.

    One observation used to arrive with the one plan that produced it. Now
    several do, and they are paired by position: the executor returns what it
    was given, in the order it was given.
    """

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        if not state.last_observations or not state.planned_tool_calls:
            return {
                "fatal_error": (
                    "evidence_normalizer entered without an observation and plan"
                ),
                "visited_nodes": [*state.visited_nodes, "evidence_normalizer"],
            }

        evidence = list(state.evidence)
        unknowns = list(state.unknowns)
        resolved = {item.host: item for item in state.hosts}

        for observation in state.last_observations:
            planned = _plan_for(state, observation)
            if planned is None:
                # The executor answers only what it was asked, so this is a
                # programming error rather than a planner mistake -- but it
                # costs one observation, not the investigation.
                unknowns.append(
                    UnknownItem(
                        code="observation_unplanned",
                        message=(
                            f"{observation.tool_name} returned an answer no plan"
                            " in this turn asked for"
                        ),
                        tool_call_id=observation.tool_call_id,
                    ),
                )
                continue
            host = resolved.get(planned.host or "") or (
                state.hosts[0] if len(state.hosts) == 1 else None
            )
            if host is None:
                unknowns.append(
                    UnknownItem(
                        code="observation_unhosted",
                        message=(
                            f"{observation.tool_name}: no resolved host could be"
                            " associated with the answer"
                        ),
                        tool_call_id=observation.tool_call_id,
                    ),
                )
                continue
            evidence, merge_unknowns = merge_evidence(
                evidence,
                normalize_observation(
                    observation,
                    planned,
                    host_id=host.host_id,
                    host=host.host,
                ),
            )
            unknowns.extend(merge_unknowns)

        return {
            "evidence": evidence,
            "unknowns": unknowns,
            "visited_nodes": [*state.visited_nodes, "evidence_normalizer"],
        }


def _plan_for(state: InvestigationState, observation: Any) -> Any:
    """The call that produced this answer, matched on the tool and its purpose.

    The executor hands back what it was given in the order it was given, so
    position would do -- but a call that raised leaves a gap, and pairing by
    position after a gap files an answer under the wrong question.
    """
    for planned in state.planned_tool_calls:
        if (
            planned.tool_name == observation.tool_name
            and state.tool_call_purposes.get(observation.tool_call_id)
            == planned.purpose
        ):
            return planned
    for planned in state.planned_tool_calls:
        if planned.tool_name == observation.tool_name:
            return planned
    return None


class StopGuardNode:
    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        return {
            **hard_stop_update(state),
            "visited_nodes": [*state.visited_nodes, "stop_guard"],
        }


def _response_chars(result: Any) -> int:
    """How much answer came back, measured the way it will be sent."""
    if result.response is None:
        return 0
    return len(json.dumps(result.response, ensure_ascii=False, default=str))


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


def _prompt(*names: str) -> str:
    """One prompt, or several joined into one.

    Knowledge about the log store belongs to whichever node queries it, which is
    three of them. Written out three times it would drift in two of them, so it
    is a file the nodes that need it compose in.
    """
    separator = "\n\n"
    return separator.join(
        (files("aiops_rca.prompts") / name).read_text(encoding="utf-8")
        for name in names
    )


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
