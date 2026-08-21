"""The planner may only name a tool the router can actually call.

It used to name an *effect* -- "related_events", "audit_actor" -- and a routing
table turned that into a tool. The indirection was a second vocabulary to keep
in step with the first, and it failed in both directions. gpt-5.4-mini ended an
investigation with

    no allowed tool can produce effect
    'related_events around the target window'

which reads like a missing capability and was not one: the effect was
registered, and the planner had appended a qualifier to its name. Later the same
message covered a different case entirely -- the tool existed and routed, but no
arguments had been proposed for it -- and a report repeated that as a limitation
of the Zabbix tooling.

Naming the tool removes the vocabulary and both failures with it.
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
    ToolPolicyError,
)

OBSERVATION = {
    "question": "서비스가 멈추기 전에 누가 명령을 실행했는가",
    "discriminates_hypothesis_ids": ["H1"],
    "expected_if_true": [],
    "expected_if_false": [],
    "temporal_scope": "historical",
    "host": "vm-1",
    "arguments_json": "{}",
    "generic_fallback_allowed": False,
}


def decision(**overrides):
    """A turn that asks one question, however that turn is shaped."""
    return {
        "observations": [{**OBSERVATION, **overrides}],
        "stop_reason": None,
    }


def test_every_offered_name_is_a_tool_that_exists():
    names = DEFAULT_TOOL_REGISTRY.names()
    # Each one has to be callable, or the planner could name something that
    # validates and then routes nowhere.
    for name in names:
        assert DEFAULT_TOOL_REGISTRY.get(name).name == name
    assert "get_wazuh_alert_summary" in names
    assert "get_related_events" in names


def test_a_registered_tool_is_accepted():
    bound = observation_decision_for(DEFAULT_TOOL_REGISTRY.names())
    parsed = bound.model_validate(decision(required_tool="get_related_events"))
    assert parsed.observations[0].required_tool == "get_related_events"


def test_a_qualified_name_is_refused():
    # The failure that started this: a real name with a phrase appended.
    bound = observation_decision_for(DEFAULT_TOOL_REGISTRY.names())
    with pytest.raises(ValidationError):
        bound.model_validate(
            decision(required_tool="get_related_events around the target window"),
        )


def test_a_tool_that_is_not_allowlisted_is_refused():
    bound = observation_decision_for(DEFAULT_TOOL_REGISTRY.names())
    with pytest.raises(ValidationError):
        bound.model_validate(decision(required_tool="host.delete"))


def test_stopping_is_still_expressible():
    # Having no next observation is a legitimate answer, and narrowing the
    # field must not turn it into a validation error. An empty turn is how it
    # is said now, rather than a question with no tool attached.
    bound = observation_decision_for(DEFAULT_TOOL_REGISTRY.names())
    parsed = bound.model_validate(
        {"observations": [], "stop_reason": "더 가를 관측이 없음"},
    )
    assert parsed.observations == []
    assert parsed.stop_reason


def test_an_empty_registry_leaves_the_contract_open():
    assert observation_decision_for(()) is ObservationDecision


class TestValidatingTheCall:
    """The named tool still has to be callable with the arguments proposed.

    This is the half that survives. What went away is the question of whether
    any tool *could* do the job -- the planner names one that exists, so the
    only thing left to check is whether this call is allowed to happen.
    """

    def test_a_complete_call_is_allowed(self):
        policy = DEFAULT_TOOL_REGISTRY.validate_call(
            "get_trigger_details",
            {"trigger_id": "23456"},
            RoutingContext(),
        )
        assert policy.name == "get_trigger_details"

    def test_a_missing_argument_says_which(self):
        with pytest.raises(ToolPolicyError) as error:
            DEFAULT_TOOL_REGISTRY.validate_call(
                "get_trigger_details", {}, RoutingContext()
            )
        assert "trigger_id" in str(error.value)

    def test_the_budget_is_reported_as_the_budget(self):
        # Exhaustion used to arrive wearing the capability sentence, which is
        # how a run that had spent its calls read as a tool that does not exist.
        with pytest.raises(ToolPolicyError) as error:
            DEFAULT_TOOL_REGISTRY.validate_call(
                "get_trigger_details",
                {"trigger_id": "23456"},
                RoutingContext(tool_call_count=30, max_tool_calls=30),
            )
        assert "budget is exhausted" in str(error.value)

    def test_a_current_only_tool_cannot_answer_a_historical_question(self):
        # The one rule the loop cannot recover from: a list of what is running
        # now is a well-formed answer to a question about yesterday, and nothing
        # downstream can tell it apart from a right one.
        with pytest.raises(ToolPolicyError) as error:
            DEFAULT_TOOL_REGISTRY.validate_call(
                "get_wazuh_agent_processes",
                {"agent_id": "001"},
                RoutingContext(temporal_scope="historical"),
                "current_only",
            )
        assert "historical" in str(error.value)

    def test_a_tool_that_declares_nothing_is_not_restricted(self):
        # Every tool was unrestricted until one said otherwise. A server that
        # declares nothing is the default, not a case to handle.
        policy = DEFAULT_TOOL_REGISTRY.validate_call(
            "get_wazuh_agent_processes",
            {"agent_id": "001"},
            RoutingContext(temporal_scope="historical"),
        )
        assert policy.name == "get_wazuh_agent_processes"

    def test_the_declaration_comes_from_the_catalog(self):
        # Read off the live catalog, so the fact belongs to the server that owns
        # the tool rather than to a table of tool names on this side.
        from aiops_rca.graph.deterministic_nodes import _catalog_scope

        catalog = [
            {"name": "get_wazuh_agent_processes", "temporal_scope": "current_only"},
            {"name": "get_wazuh_agents"},
        ]
        assert _catalog_scope(catalog, "get_wazuh_agent_processes") == "current_only"
        assert _catalog_scope(catalog, "get_wazuh_agents") == "any"
        assert _catalog_scope(catalog, "never_heard_of_it") == "any"

    def test_a_generic_tool_needs_the_gate_open(self):
        with pytest.raises(ToolPolicyError) as error:
            DEFAULT_TOOL_REGISTRY.validate_call(
                "query_zabbix",
                {"method": "host.get"},
                RoutingContext(generic_fallback_allowed=False),
            )
        assert "generic" in str(error.value)
