import pytest

from aiops_rca.tools.registry import (
    DEFAULT_TOOL_REGISTRY,
    RoutingContext,
    ToolPolicyError,
)


def test_current_state_tool_is_blocked_for_historical_question():
    with pytest.raises(ToolPolicyError, match="cannot prove historical state"):
        DEFAULT_TOOL_REGISTRY.validate_call(
            "get_wazuh_agent_processes",
            {"agent_id": "001"},
            RoutingContext(temporal_scope="historical"),
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


def test_log_evidence_routes_to_official_elasticsearch_search():
    policy = DEFAULT_TOOL_REGISTRY.route_effect(
        "raw_log_evidence",
        {
            "search": {"index": "vm-logs-*", "query_body": {"size": 10}},
        },
        RoutingContext(temporal_scope="historical", generic_fallback_allowed=True),
    )
    assert policy.name == "search"
