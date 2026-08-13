from datetime import UTC, datetime, timedelta

from conftest import make_state

from aiops_rca.graph.routing import hard_stop_update
from aiops_rca.schemas.investigation import ResolvedHost
from aiops_rca.tools.result import ToolExecutionResult


def test_tool_budget_is_a_hard_stop():
    now = datetime(2026, 8, 12, 3, tzinfo=UTC)
    result = ToolExecutionResult(
        tool_call_id="call-1",
        tool_name="get_incident_events",
        source="zabbix",
        status="empty",
        request={},
        response={"events": []},
        started_at=now,
        finished_at=now,
    )
    state = make_state(
        hosts=[ResolvedHost(host="vm-java-docker-2", host_id="11094")],
        tool_results=[result],
        tool_call_count=1,
        limits={"max_tool_calls": 1, "max_iterations": 10, "max_duration_seconds": 300},
    )
    assert hard_stop_update(state, now=now) == {
        "stop_reason": "maximum tool-call budget reached",
        "limit_reached": True,
    }


def test_duration_is_a_hard_stop():
    started = datetime(2026, 8, 12, 2, tzinfo=UTC)
    state = make_state(
        hosts=[ResolvedHost(host="vm-java-docker-2", host_id="11094")],
        started_at=started,
        limits={"max_tool_calls": 30, "max_iterations": 10, "max_duration_seconds": 60},
    )
    update = hard_stop_update(state, now=started + timedelta(seconds=61))
    assert update == {
        "stop_reason": "maximum investigation duration reached",
        "limit_reached": True,
    }
