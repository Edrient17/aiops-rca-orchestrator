"""Establishing what was observed, from whichever source can say.

This node asked Zabbix for incident events and nothing else. A host Zabbix has
no id for therefore got no phenomenon at all, and the call it could not make
raised out of the graph as a 500 -- both symptoms of one node being bound to one
tool. A guard that skipped such hosts would have hidden the second symptom and
kept the first.

It plans its lookups now. One model turn names a tool per host; the registry
validates each call; a refusal is recorded rather than raised.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from conftest import make_state

from aiops_rca.graph.live_nodes import EstablishPhenomenonNode
from aiops_rca.schemas.investigation import ResolvedHost
from aiops_rca.services.model_contracts import (
    PhenomenonDecision,
    PhenomenonScan,
    PhenomenonScanPlan,
)
from aiops_rca.tools.registry import ToolPolicyError
from aiops_rca.tools.result import ToolExecutionResult

ZABBIX_HOST = ResolvedHost(host="vm-known", host_id="11094")
LOG_ONLY_HOST = ResolvedHost(host="vm-ghost", host_id=None, found_by="search")

WINDOW = {
    "resolved_window": {"from": "2026-08-20T00:00:00Z", "to": "2026-08-21T00:00:00Z"},
}


def _result(tool_name: str, response: Any) -> ToolExecutionResult:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    return ToolExecutionResult(
        tool_call_id=f"call-{tool_name}",
        tool_name=tool_name,
        source="zabbix",
        status="ok",
        request={},
        response=response,
        started_at=now,
        finished_at=now,
    )


class PlanningModel:
    """Returns a scan plan, then the phenomenon sentence."""

    def __init__(self, plan: PhenomenonScanPlan) -> None:
        self.plan = plan
        self.payloads: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> Any:
        self.payloads.append(kwargs["payload"])
        if len(self.payloads) == 1:
            return self.plan
        return PhenomenonDecision(phenomenon="무언가 관찰됨")


class Executor:
    def __init__(self, refuse: bool = False) -> None:
        self.calls: list[Any] = []
        self.refuse = refuse

    async def execute(self, planned: Any, _context: Any) -> ToolExecutionResult:
        self.calls.append(planned)
        if self.refuse:
            raise ToolPolicyError(
                f"{planned.tool_name} is missing required arguments: host_id"
            )
        return _result(planned.tool_name, {"events": []})


def _run(plan: PhenomenonScanPlan, hosts: list[ResolvedHost], refuse: bool = False):
    model = PlanningModel(plan)
    executor = Executor(refuse=refuse)
    node = EstablishPhenomenonNode(
        model=model, model_name="stub", executor=executor
    )
    state = make_state(hosts=hosts, collection=WINDOW)
    return dict(asyncio.run(node(state))), model, executor


def _scan(host: str, tool_name: str, **arguments: Any) -> PhenomenonScan:
    import json

    return PhenomenonScan(
        host=host, tool_name=tool_name, arguments_json=json.dumps(arguments)
    )


class TestPlanningTheScan:
    def test_the_named_tool_is_what_gets_called(self):
        plan = PhenomenonScanPlan(
            scans=[_scan("vm-known", "get_incident_events", host_id="11094")],
            stop_reason=None,
        )
        _update, _model, executor = _run(plan, [ZABBIX_HOST])
        assert [call.tool_name for call in executor.calls] == ["get_incident_events"]

    def test_a_host_with_no_zabbix_id_is_scanned_somewhere_else(self):
        # The whole point. Before, this host was skipped and got no phenomenon;
        # nothing here decides that, the model reads host_id: null and picks a
        # tool that does not need one.
        plan = PhenomenonScanPlan(
            scans=[_scan("vm-ghost", "search", index="vm-logs-*")],
            stop_reason=None,
        )
        update, model, executor = _run(plan, [LOG_ONLY_HOST])
        assert [call.tool_name for call in executor.calls] == ["search"]
        assert update["phenomenon"] == "무언가 관찰됨"
        # The planner is shown the missing id rather than a filtered list.
        assert model.payloads[0]["hosts"][0]["host_id"] is None

    def test_each_host_can_be_scanned_in_a_different_place(self):
        plan = PhenomenonScanPlan(
            scans=[
                _scan("vm-known", "get_incident_events", host_id="11094"),
                _scan("vm-ghost", "search", index="vm-logs-*"),
            ],
            stop_reason=None,
        )
        _update, _model, executor = _run(plan, [ZABBIX_HOST, LOG_ONLY_HOST])
        assert [call.tool_name for call in executor.calls] == [
            "get_incident_events",
            "search",
        ]

    def test_the_scanned_host_travels_with_the_call(self):
        plan = PhenomenonScanPlan(
            scans=[_scan("vm-ghost", "search", index="vm-logs-*")],
            stop_reason=None,
        )
        _update, _model, executor = _run(plan, [LOG_ONLY_HOST])
        assert executor.calls[0].host == "vm-ghost"
        assert executor.calls[0].host_id is None


class TestWhenTheScanCannotHappen:
    def test_a_refusal_is_recorded_rather_than_raised(self):
        # This raised out of the graph as a 500, which the dispatcher then
        # retried -- and each retry posted another acknowledgement.
        plan = PhenomenonScanPlan(
            scans=[_scan("vm-ghost", "get_incident_events")],
            stop_reason=None,
        )
        update, _model, _executor = _run(plan, [LOG_ONLY_HOST], refuse=True)
        codes = [item.code for item in update["unknowns"]]
        assert "phenomenon_scan_blocked" in codes

    def test_a_plan_naming_an_unresolved_host_is_recorded(self):
        plan = PhenomenonScanPlan(
            scans=[_scan("somewhere-else", "search", index="vm-logs-*")],
            stop_reason=None,
        )
        update, _model, executor = _run(plan, [ZABBIX_HOST])
        assert executor.calls == []
        assert "phenomenon_scan_unresolved_host" in [
            item.code for item in update["unknowns"]
        ]

    def test_unreadable_arguments_skip_that_scan_only(self):
        plan = PhenomenonScanPlan(
            scans=[
                PhenomenonScan(
                    host="vm-known", tool_name="get_incident_events",
                    arguments_json="not json",
                ),
                _scan("vm-ghost", "search", index="vm-logs-*"),
            ],
            stop_reason=None,
        )
        update, _model, executor = _run(plan, [ZABBIX_HOST, LOG_ONLY_HOST])
        assert [call.tool_name for call in executor.calls] == ["search"]
        assert "phenomenon_scan_unusable" in [i.code for i in update["unknowns"]]

    def test_an_empty_plan_still_produces_a_phenomenon(self):
        # Nothing to scan is not nothing to say: the node still reports what the
        # investigation is about, and the report explains the rest.
        plan = PhenomenonScanPlan(scans=[], stop_reason="조회할 것이 없음")
        update, _model, executor = _run(plan, [LOG_ONLY_HOST])
        assert executor.calls == []
        assert update["phenomenon"] == "무언가 관찰됨"
