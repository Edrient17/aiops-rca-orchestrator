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


class ToolCandidate(StrictModel):
    tool_name: Annotated[str, Field(min_length=1, max_length=100)]
    host_id: ZabbixId | None
    arguments_json: Annotated[str, Field(min_length=2, max_length=12_000)]


class ObservationDecision(StrictModel):
    question: Annotated[str, Field(min_length=1, max_length=2000)] | None
    discriminates_hypothesis_ids: Annotated[list[str], Field(max_length=20)]
    expected_if_true: Annotated[list[HypothesisExpectation], Field(max_length=20)]
    expected_if_false: Annotated[list[HypothesisExpectation], Field(max_length=20)]
    temporal_scope: Literal["historical", "current", "timeless"]
    required_tool: Annotated[str, Field(min_length=1, max_length=100)] | None
    candidates: Annotated[list[ToolCandidate], Field(max_length=20)]
    generic_fallback_allowed: bool
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
    return create_model(
        "RoutableObservationDecision",
        __base__=ObservationDecision,
        required_tool=(Literal[tools] | None, ...),  # type: ignore[valid-type]
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
