"""Narrow LLM outputs that are converted into graph state deterministically."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, create_model

from aiops_rca.schemas.base import StrictModel, ZabbixId
from aiops_rca.schemas.investigation import Hypothesis


class PhenomenonDecision(StrictModel):
    phenomenon: Annotated[str, Field(min_length=1, max_length=2000)]


class HypothesisPlan(StrictModel):
    hypotheses: Annotated[list[Hypothesis], Field(max_length=20)]
    stop_reason: Annotated[str, Field(min_length=1, max_length=2000)] | None


class HypothesisExpectation(StrictModel):
    hypothesis_id: Annotated[str, Field(min_length=1, max_length=100)]
    prediction: Annotated[str, Field(min_length=1, max_length=1000)]


class PlannedObservation(StrictModel):
    """One question and the single call that answers it.

    This used to be a question plus a list of `candidates` for it -- alternative
    calls of which the router took whichever matched `required_tool`. The
    alternatives were never the useful axis: what an investigation wants is
    several *different* questions, and it was asking them one per cycle, each
    waiting on the previous answer it did not depend on.
    """

    question: Annotated[str, Field(min_length=1, max_length=2000)]
    discriminates_hypothesis_ids: Annotated[list[str], Field(max_length=20)]
    expected_if_true: Annotated[list[HypothesisExpectation], Field(max_length=20)]
    expected_if_false: Annotated[list[HypothesisExpectation], Field(max_length=20)]
    #: Per observation, not per turn: a batch may hold one question about now
    #: and one about last night, and the guard that stops a current-state tool
    #: proving history has to judge them apart.
    temporal_scope: Literal["historical", "current", "timeless"]
    required_tool: Annotated[str, Field(min_length=1, max_length=100)]
    #: Which resolved host this call is about, by name. The name is what the
    #: sources share; a host found in a log search has no Zabbix id to give.
    host: Annotated[str, Field(min_length=1, max_length=255)] | None
    arguments_json: Annotated[str, Field(min_length=2, max_length=12_000)]
    generic_fallback_allowed: bool


class ObservationDecision(StrictModel):
    #: Independent questions, asked together. Tool calls cost tenths of a second
    #: and the model turns around them cost twenty-six, so a sweep that asks
    #: four independent things one per cycle spends its whole latency waiting to
    #: be allowed to ask the next one.
    observations: Annotated[list[PlannedObservation], Field(max_length=8)]
    stop_reason: Annotated[str, Field(min_length=1, max_length=2000)] | None


@lru_cache(maxsize=8)
def observation_decision_for(tools: tuple[str, ...]) -> type[ObservationDecision]:
    """ObservationDecision whose required_tool must name a tool that exists.

    The planner used to name an *effect* -- "related_events", "audit_actor" --
    and a routing table turned that into a tool. The indirection was a second
    vocabulary to keep in step with the first: every tool had to declare what it
    produced, every report section had to declare what it needed, and a planner
    that wrote "related_events around the target window" routed to nothing and
    reported it as a missing capability.

    Naming the tool removes the vocabulary. The valid names are the allowlist,
    which is known here, so they are offered as the only ones the model can
    produce.
    """
    if not tools:
        return ObservationDecision
    bound = create_model(
        "RoutableObservation",
        __base__=PlannedObservation,
        required_tool=(Literal[tools], ...),  # type: ignore[valid-type]
    )
    return create_model(
        "RoutableObservationDecision",
        __base__=ObservationDecision,
        observations=(Annotated[list[bound], Field(max_length=8)], ...),
    )


class HypothesisUpdate(StrictModel):
    hypothesis_id: Annotated[str, Field(min_length=1, max_length=100)]
    status: Literal["active", "supported", "rejected", "unresolved"]
    supporting_evidence_ids: Annotated[list[str], Field(max_length=50)]
    counter_evidence_ids: Annotated[list[str], Field(max_length=50)]
    rationale: Annotated[str, Field(max_length=2000)] | None


class FactDecision(StrictModel):
    fact: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_ids: Annotated[list[str], Field(min_length=1, max_length=50)]


class HypothesisUpdateDecision(StrictModel):
    updates: Annotated[list[HypothesisUpdate], Field(max_length=20)]
    new_hypotheses: Annotated[list[Hypothesis], Field(max_length=10)]
    new_facts: Annotated[list[FactDecision], Field(max_length=20)]
    stop_reason: Annotated[str, Field(min_length=1, max_length=2000)] | None


@lru_cache(maxsize=64)
def hypothesis_update_decision_for(
    evidence_ids: tuple[str, ...],
    hypothesis_ids: tuple[str, ...],
) -> type["HypothesisUpdateDecision"]:
    """The update contract narrowed to the ids that exist right now.

    Evidence ids are generated -- prefix, host, fingerprint -- so citing one
    means reproducing a string, and a near miss is indistinguishable from an
    invention by the time it reaches the validator. Offering the real ids as
    the only permitted values removes the transcription step.
    """
    if not evidence_ids or not hypothesis_ids:
        return HypothesisUpdateDecision
    evidence = Annotated[list[Literal[evidence_ids]], Field(max_length=50)]  # type: ignore[valid-type]
    bound_update = create_model(
        "BoundHypothesisUpdate",
        __base__=HypothesisUpdate,
        hypothesis_id=(Literal[hypothesis_ids], ...),  # type: ignore[valid-type]
        supporting_evidence_ids=(evidence, ...),
        counter_evidence_ids=(evidence, ...),
    )
    return create_model(
        "BoundHypothesisUpdateDecision",
        __base__=HypothesisUpdateDecision,
        updates=(Annotated[list[bound_update], Field(max_length=20)], ...),
    )


class DiscoveredHost(StrictModel):
    """A host the search found, named the way the source named it."""

    host: Annotated[str, Field(min_length=1, max_length=255)]
    #: Only when the search actually returned one. A log line or an agent
    #: record does not carry Zabbix's id, and inventing one would put a string
    #: into every later Zabbix call that Zabbix has never heard of.
    host_id: ZabbixId | None
    #: Which tool produced the name, so a report can say where it came from.
    found_by: Annotated[str, Field(min_length=1, max_length=100)]


class HostSearchDecision(StrictModel):
    """Either the hosts found so far, or another place to look."""

    hosts: Annotated[list[DiscoveredHost], Field(max_length=20)]
    tool_name: Annotated[str, Field(min_length=1, max_length=100)] | None
    arguments_json: Annotated[str, Field(min_length=2, max_length=4000)]
    stop_reason: Annotated[str, Field(min_length=1, max_length=1000)] | None


@lru_cache(maxsize=8)
def host_search_decision_for(tools: tuple[str, ...]) -> type[HostSearchDecision]:
    """HostSearchDecision whose tool_name must name a tool that exists.

    The same binding the observation planner gets, for the same reason: a name
    the router cannot call is indistinguishable from a capability the platform
    lacks by the time it reaches a report.
    """
    if not tools:
        return HostSearchDecision
    return create_model(
        "BoundHostSearchDecision",
        __base__=HostSearchDecision,
        tool_name=(Literal[tools] | None, ...),  # type: ignore[valid-type]
    )


class PhenomenonScan(StrictModel):
    """One lookup that would show whether anything happened to one host."""

    host: Annotated[str, Field(min_length=1, max_length=255)]
    tool_name: Annotated[str, Field(min_length=1, max_length=100)]
    arguments_json: Annotated[str, Field(min_length=2, max_length=4000)]


class PhenomenonScanPlan(StrictModel):
    """Which source to ask about each host before any reasoning starts.

    A host is not always a Zabbix host. One found in a log index or an agent
    list has no Zabbix id, and the events tool requires one -- so the scan that
    establishes what was observed cannot be the same call every time.
    """

    scans: Annotated[list[PhenomenonScan], Field(max_length=20)]
    stop_reason: Annotated[str, Field(min_length=1, max_length=1000)] | None


@lru_cache(maxsize=8)
def phenomenon_scan_plan_for(tools: tuple[str, ...]) -> type[PhenomenonScanPlan]:
    """PhenomenonScanPlan whose tool names must be tools that exist."""
    if not tools:
        return PhenomenonScanPlan
    bound_scan = create_model(
        "BoundPhenomenonScan",
        __base__=PhenomenonScan,
        tool_name=(Literal[tools], ...),  # type: ignore[valid-type]
    )
    return create_model(
        "BoundPhenomenonScanPlan",
        __base__=PhenomenonScanPlan,
        scans=(Annotated[list[bound_scan], Field(max_length=20)], ...),
    )
