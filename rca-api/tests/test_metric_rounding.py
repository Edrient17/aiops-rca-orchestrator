"""Aggregate values reach the report at a precision a person can read.

A monthly report compared a filesystem "월초 3.230541%에서 월말 3.300203%로" and
reported a buffer of "377053866.666667B". Those trailing digits are the
remainder of dividing by a sample count, not a measurement, and they cost the
reader the comparison each sentence exists to make.

Rounding belongs here rather than in the MCP, which would lose the precision
for every other caller, or in the writer prompt, which would hand arithmetic to
the model this design deliberately keeps it away from.
"""

from datetime import UTC, datetime

import pytest

from aiops_rca.schemas.investigation import PlannedToolCall
from aiops_rca.tools.normalizer import normalize_observation
from aiops_rca.tools.result import ToolExecutionResult

PLANNED = PlannedToolCall(
    tool_name="get_metric_summary",
    arguments={"host_id": "10663"},
    purpose="지난달 용량 추세",
    target_hypothesis_ids=[],
    host_id="10663",
)


def _summary(**summary):
    response = {
        "series": [
            {
                "item": {
                    "item_id": "42269",
                    "name": "/: Space utilization",
                    "unit": "%",
                    "key": "vfs.fs.dependent.size[/,pused]",
                },
                "summary": {"trend": "stable", **summary},
                "data_quality": {
                    "data_source": "trends",
                    "sample_count": 19,
                    "coverage_ratio": 0.6129032258064516,
                    "partial": True,
                },
            },
        ],
    }
    result = ToolExecutionResult(
        tool_call_id="call-1",
        tool_name="get_metric_summary",
        source="zabbix",
        status="ok",
        request={"host_id": "10663"},
        response=response,
        started_at=datetime(2026, 8, 18, tzinfo=UTC),
        finished_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    return normalize_observation(result, PLANNED, host_id="10663", host="midibus")[0]


def test_a_percentage_keeps_three_decimals():
    evidence = _summary(first=3.230541284403669, last=3.3002029570815452)
    assert evidence.metric.first == 3.231
    assert evidence.metric.last == 3.3
    # The rendered summary is built from the same values, so both agree.
    assert "first=3.231" in evidence.summary
    assert "3.230541" not in evidence.summary


def test_a_byte_count_loses_its_meaningless_fraction():
    evidence = _summary(first=377053866.666667, last=541441706.666667)
    assert evidence.metric.first == 377053867
    assert evidence.metric.last == 541441707
    assert ".666667" not in evidence.summary


def test_a_change_percent_is_rounded_in_both_directions():
    assert _summary(change_percent=43.59797235023042).metric.change_percent == 43.598
    assert _summary(change_percent=-0.05476149).metric.change_percent == -0.055


def test_a_very_small_value_is_not_rounded_away_to_nothing():
    # 0.001065% is a filesystem that is essentially empty; three decimals keep
    # it distinguishable from a true zero.
    evidence = _summary(first=0.0010654321)
    assert evidence.metric.first == 0.001
    assert evidence.metric.first != 0


def test_the_coverage_ratio_the_report_quotes_is_rounded_too():
    evidence = _summary(first=1.0)
    assert evidence.data_quality.coverage_ratio == 0.613


def test_counts_and_flags_in_data_quality_are_left_alone():
    evidence = _summary(first=1.0)
    assert evidence.data_quality.sample_count == 19
    assert evidence.data_quality.partial is True
    assert evidence.data_quality.data_source == "trends"


def test_an_absent_value_stays_absent():
    # A metric with no samples reports null, which must not become 0.0 -- the
    # difference is "not measured" versus "measured as nothing".
    evidence = _summary(first=None, last=None, change_percent=None)
    assert evidence.metric.first is None
    assert evidence.metric.change_percent is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (9999.5555, 9999.556),  # below the threshold: three decimals
        (10000.4444, 10000),  # at the threshold: whole number
        (-10000.5, -10000),  # negative magnitudes use the same threshold
        (284627611648, 284627611648),  # an exact integer is unchanged
    ],
)
def test_the_whole_number_threshold(raw, expected):
    assert _summary(first=raw).metric.first == expected
