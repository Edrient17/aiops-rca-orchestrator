"""One unreadable candidate must not cost the whole investigation.

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
because one candidate out of several was misquoted.

The planner proposes alternatives so that one of them failing is survivable.
"""

import asyncio
from typing import Any

import pytest
from conftest import make_state

from aiops_rca.graph.live_nodes import ObservationPlannerNode
from aiops_rca.schemas.investigation import Hypothesis, ResolvedHost, UnknownItem
from aiops_rca.services.model_contracts import ObservationDecision, ToolCandidate
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY

HYPOTHESES = [
    Hypothesis(id="H1", statement="어제 에러가 늘었다"),
    Hypothesis(id="H2", statement="에러 수는 평소와 같다"),
]

GOOD = ToolCandidate(
    tool_name="summarize_logs",
    host_id="11094",
    arguments_json='{"host": "vm-java-docker-2", "level": "error"}',
)

# What actually arrived. A regex written into a JSON string needs its
# backslashes doubled, and the planner sent them single.
REGEX = ToolCandidate(
    tool_name="search_logs",
    host_id="11094",
    arguments_json=r'{"query": "ERROR\s+\d+"}',
)

# The other half of the same mistake: a quote inside the query closing the
# string early. This is the shape the failing run reported.
QUOTE = ToolCandidate(
    tool_name="search_logs",
    host_id="11094",
    arguments_json='{"query": "status:"500""}',
)


class StubModel:
    def __init__(self, result: Any) -> None:
        self.result = result

    async def complete(self, **_: Any) -> Any:
        return self.result


def _decide(*, candidates, unknowns=(), hypothesis_ids=("H1", "H2")):
    decision = ObservationDecision.model_validate(
        {
            "question": "어제 에러 로그가 몇 건이었는가",
            "discriminates_hypothesis_ids": list(hypothesis_ids),
            "expected_if_true": [],
            "expected_if_false": [],
            "temporal_scope": "historical",
            "required_tool": "search_logs",
            "candidates": list(candidates),
            "generic_fallback_allowed": False,
            "stop_reason": None,
        },
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
        # observation is still planned and the run goes on.
        update = _decide(candidates=[bad, GOOD])
        assert update["next_question"] is not None

    @pytest.mark.parametrize("bad", [REGEX, QUOTE], ids=["regex", "quote"])
    def test_the_readable_sibling_is_still_offered(self, bad):
        update = _decide(candidates=[bad, GOOD])
        assert "summarize_logs" in update["candidate_tool_arguments"]
        assert "search_logs" not in update["candidate_tool_arguments"]

    @pytest.mark.parametrize("bad", [REGEX, QUOTE], ids=["regex", "quote"])
    def test_dropping_it_is_recorded(self, bad):
        update = _decide(candidates=[bad, GOOD])
        assert "candidate_unusable" in _codes(update)
        message = next(
            item.message
            for item in update["unknowns"]
            if item.code == "candidate_unusable"
        )
        # Which tool, and enough of the decoder's complaint to find the spot.
        assert "search_logs" in message
        assert "not valid JSON" in message

    def test_every_candidate_failing_still_returns(self):
        # The router is where "nothing routable" gets decided, and it says so
        # accurately now. This node's job is to arrive there rather than 500.
        update = _decide(candidates=[REGEX, QUOTE])
        assert update["next_question"] is not None
        assert update["candidate_tool_arguments"] == {}
        assert _codes(update).count("candidate_unusable") == 2


class TestTheOtherWaysACandidateWasFatal:
    def test_arguments_that_are_not_an_object(self):
        candidate = ToolCandidate(
            tool_name="search_logs", host_id="11094", arguments_json="[1, 2]"
        )
        update = _decide(candidates=[candidate, GOOD])
        message = next(
            item.message
            for item in update["unknowns"]
            if item.code == "candidate_unusable"
        )
        assert "other than an object" in message

    def test_a_host_this_investigation_never_resolved(self):
        candidate = ToolCandidate(
            tool_name="search_logs",
            host_id="99999",
            arguments_json='{"host": "somewhere-else"}',
        )
        update = _decide(candidates=[candidate, GOOD])
        assert "search_logs" not in update["candidate_tool_arguments"]
        message = next(
            item.message
            for item in update["unknowns"]
            if item.code == "candidate_unusable"
        )
        assert "99999" in message

    def test_an_observation_anchored_to_no_known_hypothesis_stops_the_loop(self):
        # Also a 500 before. Ending the run with a stated reason is the worst it
        # should ever be, because the report can then say it.
        update = _decide(candidates=[GOOD], hypothesis_ids=("H7",))
        assert update["next_question"] is None
        assert update["stop_reason"]
        assert "observation_unanchored" in _codes(update)


def test_what_was_already_unknown_is_kept():
    # The planner is handed state.unknowns on its next turn, which is how it
    # learns its own quoting was rejected. Replacing the list instead of adding
    # to it would throw that away along with everything earlier.
    prior = [UnknownItem(code="earlier", message="이전 턴에서 남은 것")]
    update = _decide(candidates=[REGEX, GOOD], unknowns=prior)
    assert _codes(update) == ["earlier", "candidate_unusable"]
