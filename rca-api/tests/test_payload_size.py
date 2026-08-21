"""What each model call is made to carry.

Tool schemas and tool responses are the two big blocks in these payloads, and
both were being paid for more than once. A live investigation spent 196,119
tokens, 55% of it in hypothesis_updater, because a hundred raw log documents
went into the prompt beside the normalised evidence built from those same
documents.
"""

import json
from datetime import UTC, datetime

from aiops_rca.graph.live_nodes import _without_body
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


class TestTheObservationTheUpdaterSees:
    def test_the_raw_body_does_not_travel(self):
        # It arrives already normalised in new_evidence, bounded and with its
        # rows counted. Sending both put the same answer in twice.
        blob = "Total results: 10000, showing 100.\n" + ("x" * 170_000)
        dumped = _without_body(observation(blob))
        assert "response" not in dumped
        assert len(json.dumps(dumped, ensure_ascii=False)) < 1_000

    def test_what_the_evidence_cannot_say_is_kept(self):
        # Which call this was, and whether it answered. An evidence item exists
        # only for a call that succeeded, so failure has to be said here.
        dumped = _without_body(observation({"hits": []}))
        assert dumped["tool_name"] == "search"
        assert dumped["status"] == "ok"
        assert dumped["tool_call_id"] == "call-1"
        assert dumped["request"] == {"index": "vm-logs-*"}

    def test_the_size_that_was_dropped_is_reported(self):
        # An empty answer and a truncated hundred-document answer are different
        # findings, and the difference would otherwise vanish with the body.
        assert _without_body(observation(None))["response_size_chars"] == 0
        assert _without_body(observation({"hits": [1, 2]}))["response_size_chars"] > 0


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
