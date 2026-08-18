"""A reply that stopped at its row limit must not read as a complete count.

The MCPs return `partial` when the limit is what ended the reply, and the
adapter already turns that into a `partial` status. Nothing carried it further:
the rows became evidence indistinguishable from a complete answer, so a monthly
report could state a total that was really the size of the limit.
"""

from datetime import UTC, datetime

import pytest
from conftest import make_state

from aiops_rca.graph.deterministic_nodes import ToolExecutorNode
from aiops_rca.schemas.investigation import (
    Hypothesis,
    PlannedToolCall,
    ResolvedHost,
)
from aiops_rca.tools.result import ToolExecutionResult

PLANNED = PlannedToolCall(
    tool_name="get_incident_events",
    arguments={
        "host_id": "11094",
        "time_from": "2026-07-01T00:00:00+09:00",
        "time_to": "2026-08-01T00:00:00+09:00",
        "policy": "long_term_capacity",
    },
    purpose="지난달에 어떤 장애가 있었는가",
    target_hypothesis_ids=["h1"],
    host_id="11094",
)


class StubExecutor:
    def __init__(self, status: str) -> None:
        self.status = status

    async def execute(self, planned, context):
        return ToolExecutionResult(
            tool_call_id="call-1",
            tool_name=planned.tool_name,
            source="zabbix",
            status=self.status,
            request=dict(planned.arguments),
            response={"events": [{"event_id": "1"}], "result_count": 100, "partial": True},
            started_at=datetime(2026, 8, 18, 0, 0, tzinfo=UTC),
            finished_at=datetime(2026, 8, 18, 0, 0, 1, tzinfo=UTC),
        )


async def _run(status: str):
    node = ToolExecutorNode(StubExecutor(status))
    return await node(
        make_state(
            planned_tool_call=PLANNED,
            hypotheses=[Hypothesis(id="h1", statement="용량 소진")],
            hosts=[ResolvedHost(host="vm-java-docker-2", host_id="11094")],
        ),
    )


@pytest.mark.asyncio
async def test_a_truncated_reply_records_that_the_count_is_a_floor():
    update = await _run("partial")
    codes = [item.code for item in update["unknowns"]]
    assert "result_truncated" in codes
    truncation = next(i for i in update["unknowns"] if i.code == "result_truncated")
    assert truncation.tool_call_id == "call-1"
    assert "lower bound" in truncation.message


@pytest.mark.asyncio
async def test_a_complete_reply_records_nothing():
    update = await _run("ok")
    assert update["unknowns"] == []


@pytest.mark.asyncio
async def test_the_rows_are_still_kept():
    # The truncation is a caveat on the answer, not a reason to discard it.
    update = await _run("partial")
    assert update["last_observation"].response["result_count"] == 100
    assert update["tool_call_count"] == 1
