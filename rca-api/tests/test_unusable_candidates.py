"""One unreadable proposal must not cost the whole investigation.

A question about yesterday's error logs ended as an HTTP 500. The planner had
proposed a call whose arguments_json contained a regex, and arguments_json is a
JSON object written inside a JSON string -- escaped twice. The backslashes did
not survive the second round, `json.loads` raised, and the exception travelled
out of the graph and out of the request:

    json.decoder.JSONDecodeError: Expecting ',' delimiter: line 1 column 619
    During task with name 'observation_planner'

Nothing after the graph call ran, so the report, the trace, and the agent-run
audit rows were never written -- `aiops_agent_runs` held no rows at all for that
request. Every tool call and model call it had already paid for was discarded
because one proposal out of several was misquoted.

A turn plans several independent questions, so one of them failing has to be
survivable.
"""

import asyncio
import json
from typing import Any

import pytest
from conftest import make_state

from aiops_rca.graph.live_nodes import ObservationPlannerNode
from aiops_rca.schemas.investigation import Hypothesis, ResolvedHost
from aiops_rca.services.model_contracts import ObservationDecision
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY

HYPOTHESES = [
    Hypothesis(id="H1", statement="어제 에러가 늘었다"),
    Hypothesis(id="H2", statement="에러 수는 평소와 같다"),
]

WINDOW = {"time_from": "2026-08-18T00:00:00Z", "time_to": "2026-08-19T00:00:00Z"}


def _observation(tool_name: str, arguments_json: str, question: str = "몇 건인가"):
    return {
        "question": question,
        "discriminates_hypothesis_ids": ["H1", "H2"],
        "expected_if_true": [],
        "expected_if_false": [],
        "temporal_scope": "historical",
        "required_tool": tool_name,
        "host": "vm-java-docker-2",
        "arguments_json": arguments_json,
        "generic_fallback_allowed": False,
    }


GOOD = _observation(
    "get_incident_events",
    json.dumps({"host": "vm-java-docker-2", **WINDOW}),
    "어제 이벤트가 있었는가",
)

# What actually arrived. A regex written into a JSON string needs its
# backslashes doubled, and the planner sent them single.
REGEX = _observation("esql", r'{"query": "ERROR\s+\d+"}')

# The other half of the same mistake: a quote inside the query closing the
# string early. This is the shape the failing run reported.
QUOTE = _observation("esql", '{"query": "status:"500""}')


class StubModel:
    def __init__(self, result: Any) -> None:
        self.result = result

    async def complete(self, **_: Any) -> Any:
        return self.result


def _decide(*, observations, unknowns=()):
    decision = ObservationDecision.model_validate(
        {"observations": list(observations), "stop_reason": None},
    )
    node = ObservationPlannerNode(
        model=StubModel(decision),
        model_name="stub",
        registry=DEFAULT_TOOL_REGISTRY,
    )
    state = make_state(
        hosts=[ResolvedHost(host="vm-java-docker-2", host_id="11094")],
        hypotheses=HYPOTHESES,
        phenomenon="어제 에러 로그를 세어야 한다",
        unknowns=list(unknowns),
        collection={
            "resolved_window": {
                "from": "2026-08-18T00:00:00Z",
                "to": "2026-08-19T00:00:00Z",
            },
        },
    )
    return asyncio.run(node(state))


def _codes(update):
    return [item.code for item in update["unknowns"]]


class TestAMisquotedArgument:
    @pytest.mark.parametrize("bad", [REGEX, QUOTE], ids=["regex", "quote"])
    def test_the_turn_survives_it(self, bad):
        # Not "does not raise the same error" -- does not raise at all. The
        # rest of the batch is still planned and the run goes on.
        update = _decide(observations=[bad, GOOD])
        assert update["next_questions"]

    @pytest.mark.parametrize("bad", [REGEX, QUOTE], ids=["regex", "quote"])
    def test_the_readable_question_is_still_asked(self, bad):
        update = _decide(observations=[bad, GOOD])
        assert [q.required_tool for q in update["next_questions"]] == [
            "get_incident_events"
        ]

    @pytest.mark.parametrize("bad", [REGEX, QUOTE], ids=["regex", "quote"])
    def test_dropping_it_is_recorded(self, bad):
        update = _decide(observations=[bad, GOOD])
        assert "candidate_unusable" in _codes(update)
        message = next(
            item.message
            for item in update["unknowns"]
            if item.code == "candidate_unusable"
        )
        # Which tool, and enough of the decoder's complaint to find the spot.
        assert "esql" in message
        assert "not valid JSON" in message

    def test_every_proposal_failing_still_returns(self):
        # This node's job is to arrive at a stop rather than to 500.
        update = _decide(observations=[REGEX, QUOTE])
        assert update["next_questions"] == []
        assert update["stop_reason"]
        assert _codes(update).count("candidate_unusable") == 2


class TestAProposalNoHypothesisAsksFor:
    def test_it_is_dropped_and_the_rest_of_the_turn_stands(self):
        unanchored = {**GOOD, "discriminates_hypothesis_ids": ["H9"]}
        update = _decide(observations=[unanchored, REGEX, GOOD])
        assert "observation_unanchored" in _codes(update)
        assert len(update["next_questions"]) == 1
