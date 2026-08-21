"""Model-backed and package-building nodes for the live collector graph."""

import json
from collections.abc import Mapping
from importlib.resources import files
from typing import Any

from aiops_rca.graph.state import InvestigationState
from aiops_rca.schemas.evidence_package import EvidencePackage
from aiops_rca.schemas.investigation import (
    Hypothesis,
    KnownFact,
    ObservationQuestion,
    PlannedToolCall,
    UnknownItem,
)
from aiops_rca.services.llm import StructuredModel
from aiops_rca.services.model_contracts import (
    HypothesisPlan,
    PhenomenonDecision,
    ToolCandidate,
    hypothesis_update_decision_for,
    observation_decision_for,
    phenomenon_scan_plan_for,
)
from aiops_rca.tools.normalizer import merge_evidence, normalize_observation
from aiops_rca.tools.registry import (
    DEFAULT_TOOL_REGISTRY,
    RoutingContext,
    ToolPolicyError,
    ToolRegistry,
)


class EstablishPhenomenonNode:
    """Establish what was observed, from whichever source can say.

    This asked Zabbix for incident events and nothing else, so a host Zabbix has
    no id for got no phenomenon at all -- and the call it could not make raised
    out of the graph as a 500 before it was guarded. Both were symptoms of the
    node being bound to one tool.

    It plans its lookups now, the way the observation planner does: one model
    turn names a tool per host, the registry validates each call, and a host
    that only exists in an agent list or a log index is scanned there. Nothing
    here knows which tools need a Zabbix id; the tools say so themselves and the
    refusal is recorded rather than raised.
    """

    def __init__(
        self,
        *,
        model: StructuredModel,
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
        evidence = list(state.evidence)
        purposes = dict(state.tool_call_purposes)
        window = _resolved_window(state)

        plan = await self.model.complete(
            model=self.model_name,
            output_type=phenomenon_scan_plan_for(self.registry.names()),
            system_prompt=_prompt("phenomenon_scan.md"),
            payload={
                "hosts": [host.model_dump(mode="json") for host in state.hosts],
                "window": window,
                "question": state.parsed_request.original_question,
                "tool_catalog": state.tool_catalog,
            },
            reasoning_effort="low",
        )

        known = {host.host: host for host in state.hosts}
        observations: list[dict[str, Any]] = []
        for scan in plan.scans:
            host = known.get(scan.host)
            if host is None:
                unknowns.append(
                    UnknownItem(
                        code="phenomenon_scan_unresolved_host",
                        message=(
                            f"The scan plan named {scan.host!r}, which this "
                            f"investigation did not resolve."
                        ),
                    ),
                )
                continue
            if len(results) >= state.limits.max_tool_calls:
                unknowns.append(
                    UnknownItem(
                        code="phenomenon_scan_budget_exhausted",
                        message="The tool-call budget ended before every host received a shallow scan.",
                    )
                )
                break
            try:
                arguments = json.loads(scan.arguments_json)
                if not isinstance(arguments, dict):
                    raise ValueError("arguments_json must decode to an object")
            except ValueError as error:
                unknowns.append(
                    UnknownItem(
                        code="phenomenon_scan_unusable",
                        message=f"{scan.tool_name}: {error}",
                    ),
                )
                continue

            planned = PlannedToolCall(
                tool_name=scan.tool_name,
                arguments=arguments,
                purpose=f"Establish the observed phenomenon on host {host.host}",
                target_hypothesis_ids=[],
                host=host.host,
                host_id=host.host_id,
            )
            try:
                result = await self.executor.execute(
                    planned,
                    RoutingContext(
                        tool_call_count=len(results),
                        max_tool_calls=state.limits.max_tool_calls,
                        # A scan reads whatever holds this host's recent
                        # activity, which for a host outside Zabbix is a raw
                        # index query by definition.
                        generic_fallback_allowed=True,
                    ),
                )
            except ToolPolicyError as error:
                # A refusal is a fact about this call, not a programming error.
                # Raising left the graph and the request with it -- a 500 that
                # discarded an investigation over one host the scan could not
                # address.
                unknowns.append(
                    UnknownItem(
                        code="phenomenon_scan_blocked",
                        message=f"{host.host}: {error}",
                    ),
                )
                continue

            results.append(result)
            purposes[result.tool_call_id] = planned.purpose
            if result.status == "error":
                errors.append(result)
                unknowns.append(
                    UnknownItem(
                        code="phenomenon_scan_error",
                        message=result.error or f"{scan.tool_name} failed",
                        tool_call_id=result.tool_call_id,
                    )
                )
            evidence, merge_unknowns = merge_evidence(
                evidence,
                normalize_observation(
                    result,
                    planned,
                    host_id=host.host_id,
                    host=host.host,
                ),
            )
            unknowns.extend(merge_unknowns)
            observations.append(
                {
                    "host": host.host,
                    "host_id": host.host_id,
                    "scanned_with": scan.tool_name,
                    "status": result.status,
                    "response": _bounded(result.response),
                    "error": result.error,
                }
            )

        decision = await self.model.complete(
            model=self.model_name,
            output_type=PhenomenonDecision,
            system_prompt=_prompt("phenomenon.md"),
            payload={
                "request": state.parsed_request.model_dump(mode="json"),
                "window": window,
                "observations": observations,
            },
            reasoning_effort="medium",
        )
        return {
            "phenomenon": decision.phenomenon,
            "evidence": evidence,
            "unknowns": unknowns,
            "tool_results": results,
            "tool_errors": errors,
            "tool_call_count": len(results),
            "tool_call_purposes": purposes,
            "last_observation": results[-1] if results else state.last_observation,
            "visited_nodes": [*state.visited_nodes, "establish_phenomenon"],
        }


class HypothesisPlannerNode:
    def __init__(self, *, model: StructuredModel, model_name: str) -> None:
        self.model = model
        self.model_name = model_name

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        decision = await self.model.complete(
            model=self.model_name,
            output_type=HypothesisPlan,
            system_prompt=_prompt("hypothesis_planner.md"),
            payload={
                "request": state.parsed_request.model_dump(mode="json"),
                "phenomenon": state.phenomenon,
                "evidence": _evidence_summaries(state),
                "unknowns": [item.model_dump(mode="json") for item in state.unknowns],
            },
            reasoning_effort="medium",
        )
        hypotheses = [
            item.model_copy(
                update={
                    "status": "active",
                    "supporting_evidence_ids": [],
                    "counter_evidence_ids": [],
                }
            )
            for item in decision.hypotheses
        ]
        _require_unique([item.id for item in hypotheses], "hypothesis id")
        # A stop_reason means stop only when there is nothing to discriminate.
        # Asked what is running on a host right now, the planner produced three
        # competing hypotheses and a stop_reason reading "the evidence so far
        # cannot determine the current state" -- which is the reason to make an
        # observation, not to stop before the first one. The prompt already says
        # stop_reason belongs to the empty-hypotheses case; this is that rule in
        # code, where a model cannot decline to follow it.
        stop_reason = decision.stop_reason if not hypotheses else None
        if not hypotheses and not stop_reason:
            stop_reason = "the request requires no causal hypothesis investigation"
        return {
            "hypotheses": hypotheses,
            "stop_reason": stop_reason,
            "visited_nodes": [*state.visited_nodes, "hypothesis_planner"],
        }


class _CandidateRejected(ValueError):
    """A planner-proposed call this graph cannot use, carrying the reason."""


def _candidate_call(
    candidate: ToolCandidate,
    state: InvestigationState,
    known_hosts: set[str],
) -> tuple[dict[str, Any], str | None]:
    """The arguments and host a proposed candidate resolves to.

    Every rejection here describes the planner's output, not this code.
    arguments_json is a JSON object written inside a JSON string, so it is
    escaped twice, and a regex argument whose backslashes did not survive the
    second round is the ordinary way it arrives malformed.

    These used to raise. The exception left the graph, left the request with it,
    and returned a 500 -- discarding the report, the trace, the agent-run audit
    rows, and every tool call already paid for, because one candidate out of
    several was unreadable. The planner proposes alternatives precisely so that
    one of them failing is survivable.
    """
    try:
        arguments = json.loads(candidate.arguments_json)
    except ValueError as error:
        raise _CandidateRejected(
            f"arguments_json is not valid JSON ({error})"
        ) from error
    if not isinstance(arguments, dict):
        raise _CandidateRejected(
            "arguments_json decoded to something other than an object"
        )

    associated = candidate.host
    if associated is None and len(state.hosts) == 1:
        associated = state.hosts[0].host
    if associated is not None and associated not in known_hosts:
        raise _CandidateRejected(
            f"host {associated!r} was not resolved by this investigation"
        )
    return arguments, associated


class ObservationPlannerNode:
    def __init__(
        self,
        *,
        model: StructuredModel,
        model_name: str,
        registry: ToolRegistry,
    ) -> None:
        self.model = model
        self.model_name = model_name
        self.registry = registry
        # Bound once: the registry is fixed for the lifetime of the service, so
        # the set of nameable tools is too.
        self.output_type = observation_decision_for(registry.names())

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        if state.stop_reason:
            return {"visited_nodes": [*state.visited_nodes, "observation_planner"]}
        decision = await self.model.complete(
            model=self.model_name,
            output_type=self.output_type,
            system_prompt=_prompt("observation_planner.md"),
            payload={
                "phenomenon": state.phenomenon,
                "hosts": [host.model_dump(mode="json") for host in state.hosts],
                "window": _resolved_window(state),
                "collection": state.collection,
                "hypotheses": [
                    item.model_dump(mode="json") for item in state.hypotheses
                ],
                "known_facts": [
                    item.model_dump(mode="json") for item in state.known_facts
                ],
                "unknowns": [item.model_dump(mode="json") for item in state.unknowns],
                "evidence": _evidence_summaries(state),
                "previous_calls": [
                    {
                        "tool_name": item.tool_name,
                        "status": item.status,
                        "request": item.request,
                    }
                    for item in state.tool_results
                ],
                "tool_catalog": state.tool_catalog
                or [policy.model_dump(mode="json") for policy in self.registry.list()],
            },
            reasoning_effort="medium",
        )
        # Same rule one stage down: a planner that names an observation and a
        # stop_reason in the same breath has described the next step, not the
        # end. Only the absence of a routable question ends the loop here.
        if not decision.question or not decision.required_tool:
            return {
                "next_question": None,
                "planned_tool_call": None,
                "stop_reason": decision.stop_reason
                or "no discriminating observation remains",
                "iteration_count": state.iteration_count + 1,
                "visited_nodes": [*state.visited_nodes, "observation_planner"],
            }

        known_hypotheses = {item.id for item in state.hypotheses}
        discriminates = [
            item
            for item in decision.discriminates_hypothesis_ids
            if item in known_hypotheses
        ]
        if not discriminates:
            # Naming no hypothesis this graph holds is a planner mistake and not
            # a programming error. Raising it took the request down with it.
            return {
                "next_question": None,
                "planned_tool_call": None,
                "stop_reason": (
                    "the planned observation discriminates no hypothesis"
                    " this investigation is holding"
                ),
                "unknowns": [
                    *state.unknowns,
                    UnknownItem(
                        code="observation_unanchored",
                        message=(
                            "The next observation named hypotheses that do not"
                            " exist: "
                            + ", ".join(decision.discriminates_hypothesis_ids)
                        ),
                    ),
                ],
                "iteration_count": state.iteration_count + 1,
                "visited_nodes": [*state.visited_nodes, "observation_planner"],
            }

        unknowns = list(state.unknowns)
        arguments_by_tool: dict[str, dict[str, Any]] = {}
        hosts_by_tool: dict[str, str] = {}
        known_hosts = {host.host for host in state.hosts}
        for candidate in decision.candidates:
            try:
                arguments, associated = _candidate_call(candidate, state, known_hosts)
            except _CandidateRejected as rejection:
                # One bad candidate is not a bad turn: the planner proposes
                # several, and the router only needs one that survives.
                unknowns.append(
                    UnknownItem(
                        code="candidate_unusable",
                        message=f"{candidate.tool_name}: {rejection}",
                    ),
                )
                continue
            arguments_by_tool[candidate.tool_name] = arguments
            if associated is not None:
                hosts_by_tool[candidate.tool_name] = associated

        next_question = ObservationQuestion(
            question=decision.question,
            discriminates_hypothesis_ids=discriminates,
            expected_if_true={
                item.hypothesis_id: item.prediction
                for item in decision.expected_if_true
                if item.hypothesis_id in known_hypotheses
            },
            expected_if_false={
                item.hypothesis_id: item.prediction
                for item in decision.expected_if_false
                if item.hypothesis_id in known_hypotheses
            },
            temporal_scope=decision.temporal_scope,
            required_tool=decision.required_tool,
        )
        return {
            "next_question": next_question,
            "planned_tool_call": None,
            "candidate_tool_arguments": arguments_by_tool,
            "candidate_tool_hosts": hosts_by_tool,
            "unknowns": unknowns,
            # The gate only means anything for a tool that is an escape
            # hatch; granting it for a structured tool grants nothing.
            "generic_fallback_allowed": (
                decision.generic_fallback_allowed
                and self.registry.get(decision.required_tool).kind == "generic"
            ),
            "iteration_count": state.iteration_count + 1,
            "visited_nodes": [*state.visited_nodes, "observation_planner"],
        }


class HypothesisUpdaterNode:
    def __init__(self, *, model: StructuredModel, model_name: str) -> None:
        self.model = model
        self.model_name = model_name

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        observation = state.last_observation
        if observation is None:
            raise ValueError("hypothesis updater requires an observation")
        new_evidence = [
            item
            for item in state.evidence
            if item.tool_call_id == observation.tool_call_id
        ]
        decision = await self.model.complete(
            model=self.model_name,
            output_type=hypothesis_update_decision_for(
                tuple(item.evidence_id for item in state.evidence),
                tuple(item.id for item in state.hypotheses),
            ),
            system_prompt=_prompt("hypothesis_updater.md"),
            payload={
                "question": (
                    state.next_question.model_dump(mode="json")
                    if state.next_question
                    else None
                ),
                "observation": observation.model_dump(mode="json"),
                "new_evidence": [item.model_dump(mode="json") for item in new_evidence],
                "hypotheses": [
                    item.model_dump(mode="json") for item in state.hypotheses
                ],
            },
            reasoning_effort="medium",
        )

        unknowns = list(state.unknowns)
        known_evidence = {item.evidence_id for item in state.evidence}
        hypotheses = {item.id: item for item in state.hypotheses}
        # These citations are now drawn from a bound list, so a mismatch means
        # something upstream changed rather than the model inventing an id.
        # Either way it is one update out of a whole investigation: the bad
        # references are dropped and recorded, because raising here discards
        # every tool call already paid for -- and the request that first hit
        # this was a month-long report that left no audit trail at all.
        for update in decision.updates:
            current = hypotheses.get(update.hypothesis_id)
            if current is None:
                unknowns.append(
                    UnknownItem(
                        code="hypothesis_update_unknown_hypothesis",
                        message=(
                            f"업데이트가 존재하지 않는 가설 {update.hypothesis_id}를 "
                            f"가리켜 무시했다."
                        ),
                    )
                )
                continue
            supporting = [
                ref for ref in update.supporting_evidence_ids if ref in known_evidence
            ]
            countering = [
                ref for ref in update.counter_evidence_ids if ref in known_evidence
            ]
            dropped = (
                set(update.supporting_evidence_ids) | set(update.counter_evidence_ids)
            ) - known_evidence
            # A tool error is not an observation about the world, so nothing it
            # produced may stand as support or contradiction.
            if observation.status == "error":
                dropped |= set(supporting) | set(countering)
                supporting, countering = [], []
            if dropped:
                unknowns.append(
                    UnknownItem(
                        code="hypothesis_update_evidence_dropped",
                        message=(
                            f"가설 {current.id}의 근거 중 {sorted(dropped)}를 "
                            f"확인할 수 없어 연결하지 않았다."
                        ),
                    )
                )
            hypotheses[current.id] = current.model_copy(
                update={
                    "status": update.status,
                    "supporting_evidence_ids": supporting,
                    "counter_evidence_ids": countering,
                    "rationale": update.rationale,
                }
            )
        for item in decision.new_hypotheses:
            if item.id in hypotheses:
                unknowns.append(
                    UnknownItem(
                        code="hypothesis_id_reused",
                        message=(
                            f"새 가설이 기존 id {item.id}를 다시 써서 무시했다."
                        ),
                    )
                )
                continue
            hypotheses[item.id] = item.model_copy(
                update={
                    "status": "active",
                    "supporting_evidence_ids": [],
                    "counter_evidence_ids": [],
                }
            )

        facts = list(state.known_facts)
        known_fact_keys = {(item.fact, tuple(item.evidence_ids)) for item in facts}
        for item in decision.new_facts:
            if set(item.evidence_ids) - known_evidence:
                unknowns.append(
                    UnknownItem(
                        code="known_fact_evidence_missing",
                        message=(
                            f"사실 '{item.fact[:80]}'이 확인할 수 없는 근거를 "
                            f"가리켜 기록하지 않았다."
                        ),
                    )
                )
                continue
            key = (item.fact, tuple(item.evidence_ids))
            if key not in known_fact_keys:
                facts.append(KnownFact(fact=item.fact, evidence_ids=item.evidence_ids))
                known_fact_keys.add(key)

        return {
            "hypotheses": list(hypotheses.values()),
            "known_facts": facts,
            "unknowns": unknowns,
            "stop_reason": decision.stop_reason,
            "visited_nodes": [*state.visited_nodes, "hypothesis_updater"],
        }


class EvidencePackageBuilderNode:
    def __init__(self, registry: ToolRegistry = DEFAULT_TOOL_REGISTRY) -> None:
        self.registry = registry

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        if not state.hosts:
            return {
                "evidence_package": None,
                "visited_nodes": [*state.visited_nodes, "evidence_package_builder"],
            }
        window = _resolved_window(state)
        aggregation = (state.collection or {}).get("aggregation")
        payload = {
            "request": {
                "request_id": state.request.request_id,
                "original_question": state.parsed_request.original_question,
                "requested_by": str(
                    state.request.metadata.get("user_id") or state.request.source
                ),
            },
            "query_context": {
                "hosts": [
                    {"host": host.host, "host_id": host.host_id}
                    for host in state.hosts
                ],
                "timezone": state.parsed_request.timezone,
                "anchor_time": state.parsed_request.anchor_time
                or state.request.received_at,
            },
            "investigation": {
                "initial_window": {**window, "aggregation": aggregation},
                "final_window": {**window, "aggregation": aggregation},
                "iterations": max(1, state.iteration_count),
                "tool_calls": [
                    {
                        "tool_call_id": item.tool_call_id,
                        "tool_name": item.tool_name,
                        "purpose": state.tool_call_purposes.get(
                            item.tool_call_id, "Collect investigation evidence"
                        ),
                        "status": _package_call_status(item.status),
                    }
                    for item in state.tool_results
                ],
                "expansion_reasons": [],
                "stop_reason": state.stop_reason or "investigation completed",
                "limit_reached": state.limit_reached,
            },
            "observed_failure_mode": state.phenomenon
            or state.parsed_request.incident_description,
            "evidence": [item.model_dump(mode="json") for item in state.evidence],
            "confirmed_facts": [
                {"fact": item.fact, "evidence_refs": item.evidence_ids}
                for item in state.known_facts
            ],
            "hypotheses": [
                {
                    "description": item.statement,
                    "supporting_evidence_refs": item.supporting_evidence_ids,
                    "contradicting_evidence_refs": item.counter_evidence_ids,
                    "confidence": _confidence(item),
                }
                for item in state.hypotheses
            ],
            "unknowns": [item.message for item in state.unknowns],
        }
        package = EvidencePackage.model_validate(payload)
        return {
            "evidence_package": package,
            "visited_nodes": [*state.visited_nodes, "evidence_package_builder"],
        }


def _prompt(name: str) -> str:
    return files("aiops_rca.prompts").joinpath(name).read_text(encoding="utf-8")


def _resolved_window(state: InvestigationState) -> dict[str, str]:
    window = (state.collection or {}).get("resolved_window")
    if (
        not isinstance(window, Mapping)
        or not window.get("from")
        or not window.get("to")
    ):
        raise ValueError("collection.resolved_window is required")
    return {"from": str(window["from"]), "to": str(window["to"])}


def _evidence_summaries(state: InvestigationState) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": item.evidence_id,
            "source": item.source,
            "evidence_type": item.evidence_type,
            "summary": item.summary,
            "host_id": item.resource_ids.host_id,
            "window": item.window.model_dump(mode="json", by_alias=True)
            if item.window
            else None,
            "data_quality": item.data_quality.model_dump(mode="json")
            if item.data_quality
            else None,
        }
        for item in state.evidence
    ]


def _bounded(value: Any, *, max_chars: int = 20_000) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) <= max_chars:
        return value
    return {"truncated": True, "prefix": encoded[:max_chars]}


def _package_call_status(status: str) -> str:
    if status == "error":
        return "failed"
    if status in {"partial", "filtered_empty"}:
        return "partial"
    return "success"


def _confidence(hypothesis: Hypothesis) -> str:
    if hypothesis.status == "supported":
        return "high" if not hypothesis.counter_evidence_ids else "medium"
    if hypothesis.status == "rejected":
        return "low"
    return "medium" if hypothesis.supporting_evidence_ids else "low"


def _require_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} values must be unique")
