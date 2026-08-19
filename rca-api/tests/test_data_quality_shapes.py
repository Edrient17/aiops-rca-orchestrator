"""A tool may report quality in a shape the evidence schema cannot tag.

`query_zabbix` answers raw queries, so its data_quality describes the query --
`row_limit`, `hit_row_limit`, `restricted_to_host_groups` -- not a measurement.
Evidence.data_quality is a union discriminated on `data_source`, which that
block does not have. It was passed straight through, and the first
investigation to call query_zabbix died with `union_tag_not_found` while
writing its evidence down: everything collected was lost to a field nobody
reads for the answer.
"""

from datetime import UTC, datetime

import pytest

from aiops_rca.schemas.investigation import PlannedToolCall
from aiops_rca.tools.normalizer import normalize_observation
from aiops_rca.tools.result import ToolExecutionResult

RAW_QUERY_QUALITY = {
    "row_limit": 50,
    "hit_row_limit": False,
    "restricted_to_host_groups": ["73"],
}


def _evidence(tool_name: str, source: str, response: dict):
    result = ToolExecutionResult(
        tool_call_id="call-1",
        tool_name=tool_name,
        source=source,
        status="ok",
        request={"method": "event.get"},
        response=response,
        started_at=datetime(2026, 8, 19, tzinfo=UTC),
        finished_at=datetime(2026, 8, 19, tzinfo=UTC),
    )
    planned = PlannedToolCall(
        tool_name=tool_name,
        arguments={"method": "event.get"},
        purpose="원시 조회",
        target_hypothesis_ids=[],
        host_id="11094",
    )
    return normalize_observation(result, planned, host_id="11094", host="vm-a")[0]


def test_a_raw_query_result_becomes_evidence_instead_of_failing():
    evidence = _evidence(
        "query_zabbix", "zabbix", {"rows": [{"eventid": "1"}], "data_quality": RAW_QUERY_QUALITY}
    )
    assert evidence.evidence_id.startswith("zbx:object:")
    assert evidence.data_quality is None


def test_the_dropped_block_is_still_readable_in_the_summary():
    # Nothing a reader could have used is lost: the generic summary carries the
    # whole response, quality block included.
    evidence = _evidence(
        "query_zabbix", "zabbix", {"rows": [], "data_quality": RAW_QUERY_QUALITY}
    )
    assert "row_limit" in evidence.summary


def test_a_log_quality_block_is_kept():
    evidence = _evidence(
        "search",
        "elasticsearch",
        {"hits": [{"m": 1}], "data_quality": {"data_source": "logs", "partial": False}},
    )
    assert evidence.data_quality is not None
    assert evidence.data_quality.data_source == "logs"


def test_a_kept_block_still_gets_its_ratio_rounded():
    evidence = _evidence(
        "search",
        "elasticsearch",
        {
            "hits": [{"m": 1}],
            "data_quality": {
                "data_source": "logs",
                "partial": True,
                "sampled_fraction": 0.5,
            },
        },
    )
    assert evidence.data_quality.partial is True


@pytest.mark.parametrize(
    "quality",
    [None, "not a mapping", 42, {}, {"data_source": "something_else"}],
)
def test_anything_untaggable_is_dropped_quietly(quality):
    evidence = _evidence("query_zabbix", "zabbix", {"rows": [], "data_quality": quality})
    assert evidence.data_quality is None
