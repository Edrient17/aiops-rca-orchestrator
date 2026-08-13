"""The planner may only ask for effects the router can actually route.

gpt-5.4-mini ended an investigation with

    no allowed tool can produce effect
    'related_events around the target window'

which reads like a missing capability and is not one: related_events is
registered, and the planner had appended a qualifier to its name. route_effect
matches exactly, so the run stopped two tools in with the audit trail
unexamined.
"""

import pytest
from pydantic import ValidationError

from aiops_rca.services.model_contracts import (
    ObservationDecision,
    observation_decision_for,
)
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY

BASE = {
    "question": "서비스가 멈추기 전에 누가 명령을 실행했는가",
    "discriminates_hypothesis_ids": ["H1"],
    "expected_if_true": [],
    "expected_if_false": [],
    "temporal_scope": "historical",
    "candidates": [],
    "generic_fallback_allowed": False,
    "stop_reason": None,
}


def test_every_registered_effect_is_offered():
    effects = DEFAULT_TOOL_REGISTRY.effects()
    # Each one has to belong to some tool, or the planner could name an effect
    # that routes nowhere while looking valid.
    for effect in effects:
        assert any(
            effect in policy.effects for policy in DEFAULT_TOOL_REGISTRY.list()
        ), effect
    assert "audit_actor" in effects
    assert "related_events" in effects


def test_a_registered_effect_is_accepted():
    bound = observation_decision_for(DEFAULT_TOOL_REGISTRY.effects())
    decision = bound.model_validate({**BASE, "required_effect": "related_events"})
    assert decision.required_effect == "related_events"


def test_a_qualified_effect_name_is_refused():
    bound = observation_decision_for(DEFAULT_TOOL_REGISTRY.effects())
    with pytest.raises(ValidationError):
        bound.model_validate(
            {**BASE, "required_effect": "related_events around the target window"},
        )


def test_stopping_is_still_expressible():
    # Having no next observation is a legitimate answer, and narrowing the
    # field must not turn it into a validation error.
    bound = observation_decision_for(DEFAULT_TOOL_REGISTRY.effects())
    decision = bound.model_validate(
        {**BASE, "required_effect": None, "stop_reason": "더 가를 관측이 없음"},
    )
    assert decision.required_effect is None


def test_an_empty_registry_leaves_the_contract_open():
    assert observation_decision_for(()) is ObservationDecision
