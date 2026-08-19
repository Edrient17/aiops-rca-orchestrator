"""A summary that was cut must say it was cut.

Evidence.summary is capped by the schema and the cap was applied with a slice,
so whatever sat past it vanished without trace. A process list arrived as its
first three kilobytes of kernel threads and the report described that as though
it were the host. The same silent cut applies to any tool with a long reply --
a raw Zabbix query returning fifty rows loses its tail the same way.
"""

from datetime import UTC, datetime

import pytest

from aiops_rca.schemas.investigation import PlannedToolCall
from aiops_rca.tools.normalizer import SUMMARY_CAPACITY, normalize_observation
from aiops_rca.tools.result import ToolExecutionResult


def _evidence(response):
    result = ToolExecutionResult(
        tool_call_id="call-1",
        tool_name="query_zabbix",
        source="zabbix",
        status="ok",
        request={"method": "item.get"},
        response=response,
        started_at=datetime(2026, 8, 19, tzinfo=UTC),
        finished_at=datetime(2026, 8, 19, tzinfo=UTC),
    )
    planned = PlannedToolCall(
        tool_name="query_zabbix",
        arguments={"method": "item.get"},
        purpose="원시 조회",
        target_hypothesis_ids=[],
        host_id="11094",
    )
    return normalize_observation(result, planned, host_id="11094", host="vm-a")[0]


def test_a_long_reply_is_marked_as_cut():
    evidence = _evidence({"rows": [{"name": "x" * 200} for _ in range(50)]})
    assert len(evidence.summary) <= SUMMARY_CAPACITY
    assert "요약 잘림" in evidence.summary


def test_the_full_length_is_stated():
    # "It was cut" without "from how much" leaves the reader unable to judge
    # whether they are looking at most of the answer or a tenth of it.
    evidence = _evidence({"rows": [{"name": "x" * 200} for _ in range(50)]})
    tail = evidence.summary.rsplit("[", 1)[-1]
    assert "전체" in tail
    digits = "".join(ch for ch in tail if ch.isdigit())
    assert int(digits) > SUMMARY_CAPACITY


def test_a_short_reply_is_untouched():
    evidence = _evidence({"rows": [{"name": "small"}]})
    assert "요약 잘림" not in evidence.summary
    assert evidence.summary.endswith("}")


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
