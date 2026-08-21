from datetime import UTC, datetime

from aiops_rca.schemas.investigation import PlannedToolCall
from aiops_rca.tools.adapters.base import classify_result
from aiops_rca.tools.normalizer import merge_evidence, normalize_observation
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY
from aiops_rca.tools.result import ToolExecutionResult


def test_filtered_empty_is_distinct_from_empty(fixture_json):
    response = fixture_json("elasticsearch/filtered_empty.json")
    policy = DEFAULT_TOOL_REGISTRY.get("search")
    assert classify_result(policy, response) == ("filtered_empty", None)


def test_official_elasticsearch_result_normalizes_without_claiming_silence(
    fixture_json,
):
    now = datetime(2026, 8, 12, 3, tzinfo=UTC)
    response = fixture_json("elasticsearch/filtered_empty.json")
    result = ToolExecutionResult(
        tool_call_id="call-log-1",
        tool_name="search",
        source="elasticsearch",
        status="filtered_empty",
        request={
            "index": "vm-logs-*",
            "query_body": {
                "query": {"match": {"message": "OutOfMemory"}},
                "size": 10,
            },
        },
        response=response,
        started_at=now,
        finished_at=now,
    )
    planned = PlannedToolCall(
        tool_name="search",
        arguments=result.request,
        purpose="Did OutOfMemory appear in the incident window?",
        target_hypothesis_ids=["h1"],
    )

    evidence = normalize_observation(
        result,
        planned,
        host_id="11094",
        host="vm-java-docker-2",
    )
    assert len(evidence) == 1
    assert evidence[0].data_quality.empty_because_filtered.lines_in_window == 3820
    assert evidence[0].source == "elasticsearch"
    assert "OutOfMemory" in evidence[0].search_query


def test_official_elasticsearch_nested_empty_hits_are_empty():
    policy = DEFAULT_TOOL_REGISTRY.get("search")
    response = {"hits": {"total": {"value": 0}, "hits": []}}
    assert classify_result(policy, response) == ("empty", None)


def test_tool_error_never_becomes_evidence():
    now = datetime(2026, 8, 12, 3, tzinfo=UTC)
    result = ToolExecutionResult(
        tool_call_id="call-error",
        tool_name="get_wazuh_alert_summary",
        source="wazuh",
        status="error",
        request={
            "time_from": "2026-08-12T02:00:00Z",
            "time_to": "2026-08-12T03:00:00Z",
        },
        response=None,
        error="timeout",
        started_at=now,
        finished_at=now,
    )
    planned = PlannedToolCall(
        tool_name=result.tool_name,
        arguments=result.request,
        purpose="Who stopped the service?",
        target_hypothesis_ids=["h1"],
    )
    assert (
        normalize_observation(result, planned, host_id="11094", host="vm-java-docker-2")
        == []
    )


def test_metric_summary_copies_mcp_statistics_without_recalculation():
    now = datetime(2026, 8, 12, 3, tzinfo=UTC)
    response = {
        "host_id": "11094",
        "window": {
            "from": "2026-08-12T02:00:00Z",
            "to": "2026-08-12T03:00:00Z",
        },
        "aggregation": "5m",
        "series": [
            {
                "evidence_id": "zbx:metric:123:window",
                "item": {
                    "item_id": "123",
                    "name": "Memory utilization",
                    "key": "vm.memory.util",
                    "unit": "%",
                },
                "summary": {
                    "min": 10.0,
                    "max": 92.0,
                    "avg": 42.5,
                    "first": 20.0,
                    "last": 80.0,
                    "change_percent": 300.0,
                    "trend": "increasing",
                },
                "data_quality": {
                    "data_source": "history",
                    "sample_count": 60,
                    "returned_points": 0,
                    "expected_buckets": 12,
                    "coverage_ratio": 1.0,
                    "partial": False,
                },
            },
        ],
    }
    result = ToolExecutionResult(
        tool_call_id="call-metric",
        tool_name="get_metric_summary",
        source="zabbix",
        status="ok",
        request={},
        response=response,
        started_at=now,
        finished_at=now,
    )
    planned = PlannedToolCall(
        tool_name="get_metric_summary",
        arguments={},
        purpose="Did memory rise before the stop?",
        target_hypothesis_ids=["h1"],
    )
    evidence = normalize_observation(
        result,
        planned,
        host_id="11094",
        host="vm-java-docker-2",
    )[0]
    assert evidence.metric.avg == 42.5
    assert evidence.metric.change_percent == 300.0
    assert evidence.data_quality.sample_count == 60


def test_a_second_reading_supersedes_the_first_and_is_recorded(fixture_json):
    now = datetime(2026, 8, 12, 3, tzinfo=UTC)
    response = fixture_json("elasticsearch/filtered_empty.json")
    result = ToolExecutionResult(
        tool_call_id="call-log-1",
        tool_name="search",
        source="elasticsearch",
        status="filtered_empty",
        request={"index": "vm-logs-*", "query_body": {"size": 10}},
        response=response,
        started_at=now,
        finished_at=now,
    )
    planned = PlannedToolCall(
        tool_name="search",
        arguments=result.request,
        purpose="find lines",
        target_hypothesis_ids=["h1"],
    )
    first = normalize_observation(
        result, planned, host_id="11094", host="vm-java-docker-2"
    )[0]
    changed = first.model_copy(update={"summary": "different observation"})
    merged, unknowns = merge_evidence([first], [changed])

    # The later reading wins: it is the one still true. Raising here discarded
    # a whole investigation over two readings of the same thing, which is not a
    # programming error but an ordinary fact about time passing.
    assert [item.summary for item in merged] == ["different observation"]
    assert [item.code for item in unknowns] == ["evidence_superseded"]


class TestTheQueryStoredWithTheEvidence:
    """`search_query` reopens a citation, or it is not worth storing.

    A planner following this project's own log guidance writes several
    aggregates with a filter on each, and one such ES|QL query overran the
    1000-character bound the field had. That raised out of evidence_normalizer
    and ended an investigation whose tool calls were already paid for.
    """

    def _evidence(self, query: str):
        from datetime import UTC, datetime

        from aiops_rca.schemas.investigation import PlannedToolCall
        from aiops_rca.tools.normalizer import normalize_observation
        from aiops_rca.tools.result import ToolExecutionResult

        now = datetime(2026, 8, 21, tzinfo=UTC)
        planned = PlannedToolCall(
            tool_name="esql",
            arguments={"query": query},
            purpose="시간대별 오류 집계",
            target_hypothesis_ids=[],
        )
        result = ToolExecutionResult(
            tool_call_id="call-1",
            tool_name="esql",
            source="elasticsearch",
            status="ok",
            request={"query": query},
            response="Results\n[{\"n\": 3}]",
            started_at=now,
            finished_at=now,
        )
        return normalize_observation(result, planned, host_id=None, host="vm-1")

    def test_a_long_esql_query_is_kept(self):
        # Well past the old bound, and the shape the prompt asks for.
        query = "FROM vm-logs-* | WHERE " + " OR ".join(
            f'MATCH(message, "term{index}")' for index in range(40)
        )
        assert len(query) > 1000
        assert self._evidence(query)[0].search_query == query

    def test_a_query_too_long_to_store_costs_a_footnote_not_the_run(self):
        # Cutting it would leave something nobody can re-run, which is the only
        # reason the field exists. Dropping it loses a footnote; raising loses
        # every tool call the investigation had already made.
        query = "FROM vm-logs-* | WHERE " + ("x" * 5_000)
        evidence = self._evidence(query)
        assert evidence[0].search_query is None
        assert evidence[0].summary
