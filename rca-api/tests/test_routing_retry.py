"""A refused plan gets another turn, rather than ending the investigation.

A live run planned `get_wazuh_alert_summary` with "wazuh" in the candidate's
`host` field -- the source, where a host name belonged. That dropped the only
candidate, so the router validated an empty argument set, reported the required
arguments missing, and set a stop_reason. The investigation ended with two tool
calls and a report saying it could not check.

The writer has always been allowed a second draft against the reason its first
was rejected. This is the same turn one stage earlier: the router hands the
objection back to the planner, and only gives up once the planner has had its
attempts.
"""

import asyncio

from conftest import make_state

from aiops_rca.graph.deterministic_nodes import MAX_ROUTING_ATTEMPTS, ToolRouterNode
from aiops_rca.graph.routing import route_after_tool_router
from aiops_rca.schemas.investigation import (
    Hypothesis,
    ObservationQuestion,
    PlannedToolCall,
    ResolvedHost,
)
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY, ToolPolicyError

HOST = ResolvedHost(host="vm-known", host_id="11094")
H1 = Hypothesis(id="h1", statement="운영자가 서비스를 재시작했다")

WINDOW = {
    "time_from": "2026-08-20T00:00:00Z",
    "time_to": "2026-08-21T00:00:00Z",
}


def question(tool: str = "get_wazuh_alert_summary") -> ObservationQuestion:
    return ObservationQuestion(
        question="이 호스트에서 무엇이 실행되었는가",
        discriminates_hypothesis_ids=["h1"],
        expected_if_true={},
        expected_if_false={},
        temporal_scope="historical",
        required_tool=tool,
    )


def route(next_question_tool="get_wazuh_alert_summary", **updates):
    state = make_state(
        hosts=[HOST],
        hypotheses=[H1],
        next_question=question(next_question_tool),
        **updates,
    )
    update = dict(asyncio.run(ToolRouterNode(DEFAULT_TOOL_REGISTRY)(state)))
    return update, state


class TestARefusedPlan:
    def test_the_first_refusal_does_not_end_the_investigation(self):
        update, _ = route(candidate_tool_arguments={})
        assert update.get("stop_reason") is None
        assert update["routing_attempts"] == 1
        assert update["routing_rejections"] != []

    def test_the_objection_is_handed_back_verbatim(self):
        # The planner is told what to fix. A retry against a summary of the
        # objection is a retry against a guess.
        update, _ = route(candidate_tool_arguments={})
        assert "time_from" in update["routing_rejections"][0]

    def test_it_is_still_recorded_as_an_unknown(self):
        # routing_rejections is cleared once a plan routes; the permanent record
        # of what went wrong has to survive that.
        update, _ = route(candidate_tool_arguments={})
        assert "tool_routing_blocked" in [item.code for item in update["unknowns"]]

    def test_the_stale_question_does_not_survive_the_refusal(self):
        update, _ = route(candidate_tool_arguments={})
        assert update["next_question"] is None
        assert update["planned_tool_call"] is None

    def test_the_investigation_gives_up_once_the_attempts_are_spent(self):
        update, _ = route(
            candidate_tool_arguments={}, routing_attempts=MAX_ROUTING_ATTEMPTS
        )
        assert update["stop_reason"].startswith(
            "the next observation could not be routed"
        )


class TestAPlanThatRoutes:
    def test_it_clears_the_feedback_it_no_longer_needs(self):
        # A rejection the planner has already answered is noise in the next
        # payload, and would make a later plan look like a retry.
        update, _ = route(
            candidate_tool_arguments={"get_wazuh_alert_summary": dict(WINDOW)},
            routing_rejections=["something earlier"],
            routing_attempts=1,
        )
        assert isinstance(update["planned_tool_call"], PlannedToolCall)
        assert update["routing_rejections"] == []
        assert update["routing_attempts"] == 0


class TestWhereARefusedPlanGoes:
    def test_back_to_the_planner_while_attempts_remain(self):
        state = make_state(routing_rejections=["missing time_from"])
        assert route_after_tool_router(state) == "observation_planner"

    def test_to_the_report_once_the_router_has_given_up(self):
        state = make_state(
            routing_rejections=["missing time_from"],
            stop_reason="the next observation could not be routed: ...",
        )
        assert route_after_tool_router(state) == "evidence_package_builder"

    def test_to_the_executor_when_a_plan_survived(self):
        state = make_state(
            planned_tool_call=PlannedToolCall(
                tool_name="get_wazuh_alert_summary",
                arguments=dict(WINDOW),
                purpose="확인",
                target_hypothesis_ids=[],
            ),
        )
        assert route_after_tool_router(state) == "tool_executor"

    def test_a_fatal_error_is_not_something_to_replan_around(self):
        state = make_state(
            routing_rejections=["missing time_from"],
            fatal_error="tool_executor entered without planned_tool_call",
        )
        assert route_after_tool_router(state) == "evidence_package_builder"


class TestWhatIsNotWorthRetrying:
    """A refusal the planner can answer by asserting permission.

    The live run that motivated the retry also showed its cost: told that
    `query_zabbix` needs evidence the structured tools are insufficient, the
    planner replanned with `generic_fallback_allowed` set and spent six of ten
    calls in the escape hatch. The refusal named the condition, so it read as
    instructions. A guard the planner can open by being told it is closed is
    not a guard.
    """

    def test_the_generic_gate_is_not_reopened_by_replanning(self):
        update, _ = route(
            next_question_tool="query_zabbix",
            candidate_tool_arguments={"query_zabbix": {"method": "host.get"}},
        )
        assert update["stop_reason"].startswith(
            "the next observation could not be routed"
        )
        assert update["routing_attempts"] == 1

    def test_a_current_only_tool_cannot_replan_into_proving_history(self):
        # Same shape: the objection names temporal_scope, and a retry would
        # answer it by relabelling the question rather than by finding a tool
        # that can actually prove the claim.
        error = ToolPolicyError("x reports current state", retryable=False)
        assert error.retryable is False

    def test_a_malformed_call_is_still_worth_another_turn(self):
        update, _ = route(candidate_tool_arguments={})
        assert update.get("stop_reason") is None
