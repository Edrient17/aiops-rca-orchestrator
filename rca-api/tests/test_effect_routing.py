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
from aiops_rca.tools.registry import (
    DEFAULT_TOOL_REGISTRY,
    RoutingContext,
    ToolPolicy,
    ToolPolicyError,
    ToolRegistry,
)

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


class TestSayingWhichThingWentWrong:
    """A blocked route ends the run, so the reason has to be the real one.

    A run asking about a trigger stopped with

        no allowed tool can produce effect 'trigger_definition'

    and the report repeated it as a limitation of the Zabbix tooling.
    get_trigger_details produces that effect and was allowlisted the whole
    time; the planner had simply proposed no arguments for it. The three
    outcomes -- nothing produces it, nothing proposed it, the proposal was
    rejected -- are different facts and now read differently.
    """

    def test_nothing_producing_it_is_a_capability_gap(self):
        with pytest.raises(ToolPolicyError) as error:
            DEFAULT_TOOL_REGISTRY.route_effect("brain_scan", {}, RoutingContext())
        assert "no allowed tool can produce effect 'brain_scan'" in str(error.value)

    def test_a_tool_nobody_proposed_is_not(self):
        with pytest.raises(ToolPolicyError) as error:
            DEFAULT_TOOL_REGISTRY.route_effect(
                "trigger_definition", {}, RoutingContext()
            )
        message = str(error.value)
        assert "get_trigger_details" in message
        assert "no arguments were proposed" in message
        # The sentence that sent the last report down the wrong path.
        assert "no allowed tool can produce" not in message

    def test_a_rejected_proposal_reports_the_rejection(self):
        with pytest.raises(ToolPolicyError) as error:
            DEFAULT_TOOL_REGISTRY.route_effect(
                "trigger_definition",
                {"get_trigger_details": {}},
                RoutingContext(),
            )
        message = str(error.value)
        assert "trigger_id" in message
        assert "no allowed tool can produce" not in message

    def test_a_complete_proposal_routes(self):
        policy = DEFAULT_TOOL_REGISTRY.route_effect(
            "trigger_definition",
            {"get_trigger_details": {"trigger_id": "23456"}},
            RoutingContext(),
        )
        assert policy.name == "get_trigger_details"

    def test_several_unproposed_tools_are_all_named(self):
        registry = ToolRegistry(
            [
                ToolPolicy(name="one", source="zabbix", effects=("shared",)),
                ToolPolicy(name="two", source="wazuh", effects=("shared",)),
            ],
        )
        with pytest.raises(ToolPolicyError) as error:
            registry.route_effect("shared", {}, RoutingContext())
        message = str(error.value)
        assert "one" in message and "two" in message
        assert "any of them" in message

    def test_the_budget_is_reported_as_the_budget(self):
        # Exhaustion used to arrive wearing the capability sentence too, which
        # is how a run that had spent its calls read as a tool that does not
        # exist.
        with pytest.raises(ToolPolicyError) as error:
            DEFAULT_TOOL_REGISTRY.route_effect(
                "trigger_definition",
                {"get_trigger_details": {"trigger_id": "23456"}},
                RoutingContext(tool_call_count=30, max_tool_calls=30),
            )
        assert "budget is exhausted" in str(error.value)
