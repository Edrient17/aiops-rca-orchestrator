"""A reason not to conclude is not a reason to stop looking.

Asked what was running on a host right now, the hypothesis planner produced
three competing hypotheses and, alongside them, a stop_reason reading "the
evidence so far cannot determine the current state". That is the reason to make
an observation. The graph read the field by its name and ended the run before
the first one, so the report explained that it could not see the processes --
having never asked.

The prompt already says stop_reason belongs to the empty-hypotheses case. These
tests hold the code to it, because a model cannot be relied on to decline a
field that is offered.
"""

import asyncio
from typing import Any

from conftest import make_state

from aiops_rca.graph.live_nodes import HypothesisPlannerNode, ObservationPlannerNode
from aiops_rca.schemas.investigation import Hypothesis, ResolvedHost
from aiops_rca.services.model_contracts import (
    HypothesisPlan,
    ObservationDecision,
)
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY


class StubModel:
    def __init__(self, result: Any) -> None:
        self.result = result

    async def complete(self, **_: Any) -> Any:
        return self.result


HYPOTHESES = [
    Hypothesis(id="H1", statement="아무 일도 없었다"),
    Hypothesis(id="H2", statement="이 증거원이 못 보는 방식으로 일어났다"),
]


def _plan(hypotheses, stop_reason):
    node = HypothesisPlannerNode(
        model=StubModel(HypothesisPlan(hypotheses=hypotheses, stop_reason=stop_reason)),
        model_name="stub",
    )
    return asyncio.run(node(make_state(phenomenon="이벤트가 반환되지 않았다")))


def test_hypotheses_and_a_stop_reason_together_do_not_stop_the_run():
    update = _plan(HYPOTHESES, "증거가 부족해 현재 상태를 판별할 수 없다")
    assert update["stop_reason"] is None
    assert [h.id for h in update["hypotheses"]] == ["H1", "H2"]


def test_no_hypotheses_with_a_stop_reason_still_stops():
    update = _plan([], "요청이 인과를 묻지 않는다")
    assert update["stop_reason"] == "요청이 인과를 묻지 않는다"


def test_no_hypotheses_and_no_reason_gets_one():
    # The run has to end for a stated reason, never by simply having nothing.
    update = _plan([], None)
    assert update["stop_reason"]


class TestTheObservationPlanner:
    def _decide(self, decision):
        node = ObservationPlannerNode(
            model=StubModel(decision),
            model_name="stub",
            registry=DEFAULT_TOOL_REGISTRY,
        )
        state = make_state(
            hosts=[ResolvedHost(host="vm-java-docker-2", host_id="11094")],
            hypotheses=HYPOTHESES,
            phenomenon="이벤트가 반환되지 않았다",
            collection={
                "resolved_window": {
                    "from": "2026-08-19T02:37:14Z",
                    "to": "2026-08-19T02:47:14Z",
                },
            },
        )
        return asyncio.run(node(state))

    def _decision(self, observations=None, stop_reason=None):
        planned = {
            "question": "지금 이 호스트에서 무엇이 실행 중인가",
            "discriminates_hypothesis_ids": ["H1", "H2"],
            "expected_if_true": [],
            "expected_if_false": [],
            "temporal_scope": "current",
            "required_tool": "get_wazuh_agent_processes",
            "host": "vm-java-docker-2",
            "arguments_json": '{"agent_id": "001"}',
            "generic_fallback_allowed": False,
        }
        return ObservationDecision.model_validate(
            {
                "observations": [planned] if observations is None else observations,
                "stop_reason": stop_reason,
            },
        )

    def test_a_named_observation_survives_a_stop_reason(self):
        update = self._decide(self._decision(stop_reason="더 볼 것이 없어 보인다"))
        assert [q.required_tool for q in update["next_questions"]] == [
            "get_wazuh_agent_processes"
        ]
        assert "stop_reason" not in update

    def test_an_empty_batch_still_ends_the_loop(self):
        update = self._decide(self._decision(observations=[], stop_reason="끝"))
        assert update["next_questions"] == []
        assert update["stop_reason"] == "끝"

    def test_an_empty_batch_with_no_reason_still_ends_the_loop(self):
        update = self._decide(self._decision(observations=[]))
        assert update["next_questions"] == []
        assert update["stop_reason"]
