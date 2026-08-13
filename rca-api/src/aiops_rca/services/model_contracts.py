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
    required_effect: Annotated[str, Field(min_length=1, max_length=100)] | None
    candidates: Annotated[list[ToolCandidate], Field(max_length=20)]
    generic_fallback_allowed: bool
    stop_reason: Annotated[str, Field(min_length=1, max_length=2000)] | None


@lru_cache(maxsize=8)
def observation_decision_for(effects: tuple[str, ...]) -> type[ObservationDecision]:
    """ObservationDecision whose required_effect must name a routable effect.

    The router matches an effect exactly. A planner that answered
    "related_events around the target window" -- the registered effect with a
    qualifier attached -- routed to nothing, and the run ended with a
    stop_reason that read like a missing capability rather than a malformed
    request. The valid names are known here, so they are offered as the only
    ones the model can produce.
    """
    if not effects:
        return ObservationDecision
    return create_model(
        "RoutableObservationDecision",
        __base__=ObservationDecision,
        required_effect=(Literal[effects] | None, ...),  # type: ignore[valid-type]
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
