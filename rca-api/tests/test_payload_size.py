"""What each model call is made to carry.

Tool schemas and tool responses are the two big blocks in these payloads. A
live investigation spent 196,119 tokens, 55% of it in hypothesis_updater,
because a hundred raw log documents came back from one search.

The answer is not to send less of that answer. hypothesis_updater decides
whether the observation supports or refutes a hypothesis, so the rows are the
thing it reasons about. The answer is that a query returning a hundred
documents was the wrong question -- the same host-day is two dozen rows when
asked as an aggregate -- and the pipeline should say so rather than quietly
paying for it or quietly cutting it.
"""

from datetime import UTC, datetime

from conftest import make_state

from aiops_rca.graph.deterministic_nodes import (
    MAX_REASONABLE_RESPONSE_CHARS,
    _response_chars,
)
from aiops_rca.tools.result import ToolExecutionResult

NOW = datetime(2026, 8, 21, tzinfo=UTC)


def observation(response):
    return ToolExecutionResult(
        tool_call_id="call-1",
        tool_name="search",
        source="elasticsearch",
        status="ok",
        request={"index": "vm-logs-*"},
        response=response,
        started_at=NOW,
        finished_at=NOW,
    )


class TestAnAnswerTooLargeToReasonOver:
    def test_the_rows_still_travel_whole(self):
        # Cutting here takes away what a hypothesis is judged on. The body was
        # briefly dropped and the reasoning lost 172,000 of 175,000 characters,
        # because the evidence summary beside it caps at 3,000.
        blob = "Total results: 10000, showing 100. " + ("x" * 170_000)
        import asyncio

        from aiops_rca.graph.live_nodes import HypothesisUpdaterNode

        captured = {}

        class Model:
            async def complete(self, **kwargs):
                captured.update(kwargs["payload"])
                raise RuntimeError("stop after the payload is built")

        node = HypothesisUpdaterNode(model=Model(), model_name="stub")
        state = make_state(
            tool_results=[observation(blob)],
            last_observation=observation(blob),
            tool_call_count=1,
        )
        try:
            asyncio.run(node(state))
        except RuntimeError:
            pass
        assert blob in str(captured["observation"]["response"])

    def test_the_size_is_measured_the_way_it_will_be_sent(self):
        assert _response_chars(observation(None)) == 0
        assert _response_chars(observation({"hits": []})) > 0
        assert _response_chars(observation("x" * 100)) > 100

    def test_an_aggregate_sized_answer_is_not_flagged(self):
        rows = [{"bucket": f"2026-08-21T{hour:02d}:00:00Z", "n": 4000} for hour in range(24)]
        assert _response_chars(observation(rows)) < MAX_REASONABLE_RESPONSE_CHARS


class TestTheLogKnowledgeEveryQueryingNodeGets:
    """Three nodes query the log store, so the facts about it live in one file.

    `esql` describes itself in six words -- "Perform an Elasticsearch ES|QL
    query." -- and its only parameter is a free-text query string. Nothing tells
    a planner which indices exist, that there is no parsed level field, or that
    the three ways of matching `message` return three different numbers.
    """

    def test_the_nodes_that_query_logs_are_told_how(self):
        from aiops_rca.graph.deterministic_nodes import _prompt as host_prompt
        from aiops_rca.graph.live_nodes import _prompt as live_prompt

        for load, own in (
            (host_prompt, "host_search.md"),
            (live_prompt, "phenomenon_scan.md"),
            (live_prompt, "observation_planner.md"),
        ):
            composed = load(own, "log_queries.md")
            assert "vm-logs-*" in composed
            assert "MATCH" in composed
            assert composed.startswith(load(own).rstrip()[:40])

    def test_the_facts_in_it_are_the_measured_ones(self):
        # Numbers from the live store. If they are ever edited to something
        # nobody measured, the file stops being worth its place in the prefix.
        from aiops_rca.graph.live_nodes import _prompt

        text = _prompt("log_queries.md")
        assert 'MATCH(message, "ERROR")' in text
        assert "message.keyword" in text
        assert "STATS" in text


class TestSayingTheQueryWasShapedWrong:
    """The pipeline pays for a wall of documents either way; it can at least say so.

    Three readers want this fact: the next planner turn, which can ask again as
    an aggregate; the report, which can admit the window was surveyed rather
    than read; and whoever is looking at why an investigation cost what it did.
    """

    def _run(self, response):
        import asyncio

        from aiops_rca.graph.deterministic_nodes import ToolExecutorNode
        from aiops_rca.schemas.investigation import PlannedToolCall

        result = observation(response)

        class Executor:
            async def execute(self, _planned, _context):
                return result

        state = make_state(
            planned_tool_call=PlannedToolCall(
                tool_name="search",
                arguments={"index": "vm-logs-*"},
                purpose="로그 확인",
                target_hypothesis_ids=[],
            ),
        )
        update = asyncio.run(ToolExecutorNode(Executor())(state))
        return [item.code for item in update["unknowns"]]

    def test_a_wall_of_documents_is_announced(self):
        codes = self._run("x" * (MAX_REASONABLE_RESPONSE_CHARS + 1))
        assert "response_too_large_to_reason_over" in codes

    def test_an_aggregate_passes_without_comment(self):
        rows = [{"bucket": hour, "n": 4000} for hour in range(24)]
        assert "response_too_large_to_reason_over" not in self._run(rows)

    def test_an_empty_answer_is_not_confused_with_a_huge_one(self):
        assert "response_too_large_to_reason_over" not in self._run(None)
