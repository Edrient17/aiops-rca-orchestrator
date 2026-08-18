"""A section's declaration is collected whether or not the reasoning wants it.

A monthly capacity report ended with both of its capacity sections empty. The
investigation had found a real incident on one host and spent its iterations
reasoning about that, which is what the hypothesis loop is built to do: it
stops when no further observation discriminates between explanations. Nothing
asked whether the report could still be written.
"""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import make_state

from aiops_rca.graph.coverage_nodes import CoverageSweepNode, pending_effects
from aiops_rca.graph.routing import route_after_coverage_sweep, route_after_stop_guard
from aiops_rca.schemas.evidence_package import Evidence
from aiops_rca.schemas.investigation import Hypothesis, ResolvedHost
from aiops_rca.tools.adapters.base import AdapterSet, McpAdapter
from aiops_rca.tools.executor import ToolExecutor
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY
from aiops_rca.tools.result import ToolExecutionResult

WINDOW = {"from": "2026-07-01T00:00:00Z", "to": "2026-08-01T00:00:00Z"}
HOSTS = [
    ResolvedHost(host="vm-java-docker-2", host_id="11094"),
    ResolvedHost(host="test-java-docker-vm", host_id="11082"),
]


class ScriptedTransport:
    """Answers by tool name, and records every call that was made."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((tool_name, dict(arguments)))
        response = self.responses.get(tool_name, {})
        if isinstance(response, Exception):
            raise response
        return response


METRICS = {
    "metrics": [
        {"item_id": "120124", "name": "CPU percent usage"},
        {"item_id": "120121", "name": "Memory usage"},
    ],
}
SUMMARY = {
    "series": [
        {
            "item_id": "120124",
            "first": 10.0,
            "last": 14.0,
            "change_percent": 40.0,
            "data_quality": {
                "data_source": "trends",
                "sample_count": 720,
                "coverage_ratio": 1.0,
                "partial": False,
            },
        },
    ],
}


def build(responses: dict[str, Any] | None = None) -> tuple[CoverageSweepNode, Any]:
    transport = ScriptedTransport(
        responses
        if responses is not None
        else {"list_relevant_metrics": METRICS, "get_metric_summary": SUMMARY},
    )
    adapter = McpAdapter(
        source="zabbix",
        registry=DEFAULT_TOOL_REGISTRY,
        transport=transport,
        timeout_seconds=1,
    )
    adapters = AdapterSet(zabbix=adapter, elasticsearch=adapter, wazuh=adapter)
    executor = ToolExecutor(adapters, DEFAULT_TOOL_REGISTRY)
    return CoverageSweepNode(executor, DEFAULT_TOOL_REGISTRY), transport


def state_for(effects: list[str], **updates: Any):
    return make_state(
        hosts=HOSTS,
        collection={
            "resolved_window": WINDOW,
            "required_effects": effects,
            "metric_keywords": ["cpu", "memory", "disk"],
            "aggregation": "1d",
        },
        **updates,
    )


def test_a_declared_metric_section_is_collected_without_being_asked_for():
    node, transport = build()
    update = asyncio.run(node(state_for(["metric_change"])))

    called = [name for name, _ in transport.calls]
    # The catalog lookup is what turns keywords into item ids, so the pair is
    # the unit of collection, per host.
    assert called == [
        "list_relevant_metrics",
        "get_metric_summary",
        "list_relevant_metrics",
        "get_metric_summary",
    ]
    assert update["tool_call_count"] == 4
    assert any(e.evidence_id.startswith("zbx:metric:") for e in update["evidence"])


def test_the_declared_window_and_aggregation_are_the_ones_used():
    node, transport = build()
    asyncio.run(node(state_for(["metric_change"])))
    _, arguments = next(c for c in transport.calls if c[0] == "get_metric_summary")
    assert arguments["time_from"] == WINDOW["from"]
    assert arguments["time_to"] == WINDOW["to"]
    assert arguments["aggregation"] == "1d"
    # A month exceeds the standard policy; the adapter widens it on the way out.
    assert arguments["policy"] == "long_term_capacity"


def test_an_effect_already_observed_is_not_collected_twice():
    node, transport = build()
    # incident_events is what the phenomenon scan already produced, so the
    # sweep must recognise it as covered rather than scanning every host again.
    scan = ToolExecutionResult(
        tool_call_id="call-scan",
        tool_name="get_incident_events",
        source="zabbix",
        status="empty",
        request={"host_id": "11094"},
        response={"events": [], "result_count": 0},
        started_at=datetime(2026, 8, 18, tzinfo=UTC),
        finished_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    update = asyncio.run(
        node(
            state_for(
                ["incident_events"],
                tool_results=[scan],
                tool_call_count=1,
                evidence=[
                    Evidence.model_validate(
                        {
                            "evidence_id": "zbx:event:none:abc123",
                            "evidence_type": "observation",
                            "source": "zabbix",
                            "summary": "No Zabbix problem event was returned.",
                            "observed_at": None,
                            "window": None,
                            "resource_ids": {
                                "host_id": "11094",
                                "event_id": None,
                                "trigger_id": None,
                                "item_id": None,
                            },
                            "metric": None,
                            "data_quality": None,
                            "tool_call_id": "call-scan",
                            "search_query": None,
                        },
                    ),
                ],
            ),
        ),
    )
    assert transport.calls == []
    assert update.get("tool_call_count") is None


def test_a_failed_scan_leaves_the_effect_uncovered():
    # Looking and finding nothing is coverage; failing to look is not.
    node, transport = build({"get_incident_events": {"events": [], "result_count": 0}})
    failed = ToolExecutionResult(
        tool_call_id="call-scan",
        tool_name="get_incident_events",
        source="zabbix",
        status="error",
        request={"host_id": "11094"},
        response=None,
        error="Requested time range exceeds the standard policy",
        started_at=datetime(2026, 8, 18, tzinfo=UTC),
        finished_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    asyncio.run(
        node(
            state_for(
                ["incident_events"], tool_results=[failed], tool_call_count=1
            ),
        ),
    )
    assert [name for name, _ in transport.calls] == [
        "get_incident_events",
        "get_incident_events",
    ]


def test_nothing_declared_means_nothing_swept():
    node, transport = build()
    update = asyncio.run(node(state_for([])))
    assert transport.calls == []
    assert update["visited_nodes"][-1] == "coverage_sweep"


def test_an_uncollectable_declaration_is_recorded_rather_than_retried():
    node, _ = build({"list_relevant_metrics": ConnectionError("MCP unavailable")})
    update = asyncio.run(node(state_for(["metric_change"])))

    codes = [item.code for item in update["unknowns"]]
    assert "coverage_collection_error" in codes
    assert "declared_effect_uncovered" in codes
    # Attempted once. Without this the gate would send the run back forever.
    assert "metric_change" in update["swept_effects"]


def test_the_sweep_stops_when_the_budget_cannot_fund_a_whole_observation():
    node, transport = build()
    state = state_for(["metric_change"])
    state = state.model_copy(update={"limits": state.limits.model_copy(update={"max_tool_calls": 1})})
    asyncio.run(node(state))
    # One call would buy a catalog and no measurement, which is worse than
    # stopping: the section still cannot be written and the budget is gone.
    assert transport.calls == []


class TestTheGate:
    def test_a_run_that_stops_with_a_section_uncollected_goes_to_the_sweep(self):
        state = state_for(["metric_change"], stop_reason="더 가를 관측이 없음")
        assert route_after_stop_guard(state) == "coverage_sweep"

    def test_once_swept_the_run_is_allowed_to_finish(self):
        state = state_for(
            ["metric_change"],
            stop_reason="더 가를 관측이 없음",
            swept_effects=["metric_change"],
        )
        assert route_after_stop_guard(state) == "evidence_package_builder"

    def test_a_run_still_reasoning_is_not_diverted(self):
        assert route_after_stop_guard(state_for(["metric_change"])) == "observation_planner"

    def test_an_exhausted_budget_is_not_overridden(self):
        state = state_for(
            ["metric_change"],
            stop_reason="maximum tool-call budget reached",
            limit_reached=True,
        )
        assert route_after_stop_guard(state) == "evidence_package_builder"

    def test_the_sweep_feeds_reasoning_before_the_loop_and_the_report_after(self):
        assert route_after_coverage_sweep(state_for(["metric_change"])) == "hypothesis_planner"
        stopped = state_for(["metric_change"], stop_reason="끝")
        assert route_after_coverage_sweep(stopped) == "evidence_package_builder"


@pytest.mark.parametrize("effect", ["metric_change", "audit_actor", "incident_events"])
def test_every_effect_a_template_may_declare_has_a_recipe(effect):
    from aiops_rca.tools.coverage import obtainable_effects

    assert effect in obtainable_effects()


def test_pending_subtracts_what_is_already_known():
    state = state_for(["metric_change", "audit_actor"], swept_effects=["audit_actor"])
    assert pending_effects(state, DEFAULT_TOOL_REGISTRY) == ("metric_change",)


def test_hypotheses_do_not_affect_what_gets_collected():
    # The whole point: reasoning decided it was finished, and the declaration
    # is collected anyway.
    node, transport = build()
    state = state_for(
        ["metric_change"],
        hypotheses=[Hypothesis(id="h1", statement="용량 소진")],
        stop_reason="한 가설이 지지되고 경쟁 가설이 기각됨",
    )
    asyncio.run(node(state))
    assert [name for name, _ in transport.calls].count("get_metric_summary") == 2
