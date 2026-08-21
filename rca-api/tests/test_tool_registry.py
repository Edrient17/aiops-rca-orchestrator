import pytest

from aiops_rca.tools.registry import (
    DEFAULT_TOOL_REGISTRY,
    RoutingContext,
    ToolPolicyError,
)


def test_known_broken_tool_is_blocked():
    with pytest.raises(ToolPolicyError, match="known upstream"):
        DEFAULT_TOOL_REGISTRY.validate_call(
            "get_mappings",
            {"index": "vm-logs-*"},
            RoutingContext(generic_fallback_allowed=True),
        )


def test_generic_query_requires_explicit_fallback_authorization():
    with pytest.raises(ToolPolicyError, match="structured tools are insufficient"):
        DEFAULT_TOOL_REGISTRY.validate_call(
            "esql",
            {"query": "FROM vm-logs-* | LIMIT 10"},
            RoutingContext(),
        )

    policy = DEFAULT_TOOL_REGISTRY.validate_call(
        "esql",
        {"query": "FROM vm-logs-* | LIMIT 10"},
        RoutingContext(generic_fallback_allowed=True),
    )
    assert policy.name == "esql"


def test_metric_history_requires_scalar_item_id():
    with pytest.raises(ToolPolicyError, match="scalar"):
        DEFAULT_TOOL_REGISTRY.validate_call(
            "get_metric_history",
            {
                "host_id": "11094",
                "item_id": ["123", "124"],
                "time_from": "2026-08-12T02:00:00Z",
                "time_to": "2026-08-12T03:00:00Z",
                "aggregation": "1m",
            },
            RoutingContext(temporal_scope="historical"),
        )


def test_metric_summary_enforces_twenty_item_limit():
    with pytest.raises(ToolPolicyError, match="1 to 20"):
        DEFAULT_TOOL_REGISTRY.validate_call(
            "get_metric_summary",
            {
                "host_id": "11094",
                "item_ids": [str(index) for index in range(21)],
                "time_from": "2026-08-12T02:00:00Z",
                "time_to": "2026-08-12T03:00:00Z",
                "aggregation": "1m",
            },
            RoutingContext(temporal_scope="historical"),
        )


def test_the_official_elasticsearch_search_is_callable_with_the_gate_open():
    # A raw query is an escape hatch, so it only runs once the planner has said
    # the structured tools cannot answer.
    policy = DEFAULT_TOOL_REGISTRY.validate_call(
        "search",
        {"index": "vm-logs-*", "query_body": {"size": 10}},
        RoutingContext(temporal_scope="historical", generic_fallback_allowed=True),
    )
    assert policy.name == "search"


def test_a_host_named_by_name_is_not_refused_before_the_call():
    # The Zabbix tools take a host name as well as an id, so a host found in a
    # log index or an agent list can still be asked about. This table used to
    # require host_id and refused the call on the pipeline's own authority --
    # a copy of another server's schema, kept in step by hand until it was not.
    policy = DEFAULT_TOOL_REGISTRY.validate_call(
        "get_incident_events",
        {
            "host": "vm-java-docker-2",
            "time_from": "2026-08-12T02:00:00Z",
            "time_to": "2026-08-12T03:00:00Z",
        },
        RoutingContext(temporal_scope="historical"),
    )
    assert policy.name == "get_incident_events"


def test_arguments_the_pipeline_itself_depends_on_are_still_required():
    # The window is not the server's business alone: this service decides the
    # window and passes it, so its absence is a defect here rather than there.
    with pytest.raises(ToolPolicyError, match="time_from"):
        DEFAULT_TOOL_REGISTRY.validate_call(
            "get_incident_events",
            {"host": "vm-java-docker-2"},
            RoutingContext(temporal_scope="historical"),
        )
