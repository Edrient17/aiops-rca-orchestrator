"""A summary that was cut must say it was cut.

Evidence.summary is capped by the schema and the cap was applied with a slice,
so whatever sat past it vanished without trace. A process list arrived as its
first three kilobytes of kernel threads and the report described that as though
it were the host.

The example here used to be a raw Zabbix query, until that tool was given a list
slot of its own and its rows stopped travelling in the summary at all. What
remains on this path is the OSS Elasticsearch inventory, which answers in one
long string with the rows embedded in it -- nothing this side can lift out into
a slot, so the notice is the whole of the protection.
"""

from datetime import UTC, datetime

import pytest

from aiops_rca.schemas.investigation import PlannedToolCall
from aiops_rca.tools.normalizer import SUMMARY_CAPACITY, normalize_observation
from aiops_rca.tools.result import ToolExecutionResult


def _evidence(response):
    result = ToolExecutionResult(
        tool_call_id="call-1",
        tool_name="get_shards",
        source="elasticsearch",
        status="ok",
        request={},
        response=response,
        started_at=datetime(2026, 8, 19, tzinfo=UTC),
        finished_at=datetime(2026, 8, 19, tzinfo=UTC),
    )
    planned = PlannedToolCall(
        tool_name="get_shards",
        arguments={},
        purpose="샤드 재고",
        target_hypothesis_ids=[],
        host_id="11094",
    )
    return normalize_observation(result, planned, host_id="11094", host="vm-a")[0]


def _long_reply():
    rows = ",".join(f'{{"index":"vm-logs-{i}","shard":0}}' for i in range(200))
    return f"Found 200 shards:\n[{rows}]"


def test_a_long_reply_is_marked_as_cut():
    evidence = _evidence(_long_reply())
    assert len(evidence.summary) <= SUMMARY_CAPACITY
    assert "요약 잘림" in evidence.summary


def test_the_full_length_is_stated():
    # "It was cut" without "from how much" leaves the reader unable to judge
    # whether they are looking at most of the answer or a tenth of it.
    evidence = _evidence(_long_reply())
    tail = evidence.summary.rsplit("[", 1)[-1]
    assert "전체" in tail
    digits = "".join(ch for ch in tail if ch.isdigit())
    assert int(digits) > SUMMARY_CAPACITY


def test_a_short_reply_is_untouched():
    evidence = _evidence("Found 1 shard:\n[{\"index\":\"vm-logs\",\"shard\":0}]")
    assert "요약 잘림" not in evidence.summary
    assert "vm-logs" in evidence.summary


@pytest.mark.parametrize("size", [SUMMARY_CAPACITY - 1, SUMMARY_CAPACITY])
def test_the_boundary_does_not_add_a_notice(size):
    # Exactly at the cap nothing was lost, so nothing should be claimed.
    from aiops_rca.tools.normalizer import _bounded

    text = "a" * size
    assert _bounded(text) == text


def test_just_over_the_boundary_fits_with_its_notice():
    from aiops_rca.tools.normalizer import _bounded

    result = _bounded("a" * (SUMMARY_CAPACITY + 1))
    assert len(result) <= SUMMARY_CAPACITY
    assert "요약 잘림" in result


def test_a_tool_with_a_list_slot_does_not_reach_this_path():
    # The regression the rebase is guarding: query_zabbix used to be the example
    # above, and its rows now travel beside the sentence instead of inside it.
    result = ToolExecutionResult(
        tool_call_id="call-2",
        tool_name="query_zabbix",
        source="zabbix",
        status="ok",
        request={"method": "host.get"},
        response={"rows": [{"triggerid": str(i), "description": "x" * 120}
                           for i in range(26)]},
        started_at=datetime(2026, 8, 19, tzinfo=UTC),
        finished_at=datetime(2026, 8, 19, tzinfo=UTC),
    )
    planned = PlannedToolCall(
        tool_name="query_zabbix",
        arguments={"method": "host.get"},
        purpose="트리거 목록",
        target_hypothesis_ids=[],
        host_id="11094",
    )
    evidence = normalize_observation(result, planned, host_id="11094", host="vm-a")[0]
    assert "요약 잘림" not in evidence.summary
    assert evidence.observed is not None
    assert evidence.observed.kind == "rows"
    assert len(evidence.observed.items) == 26
    assert evidence.observed.omitted == 0
