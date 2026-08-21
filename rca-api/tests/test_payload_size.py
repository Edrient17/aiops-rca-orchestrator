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
