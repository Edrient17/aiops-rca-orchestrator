"""Finding a host that Zabbix does not know.

Host resolution was one call to `find_hosts` and nothing else, so a machine
Zabbix had never been told about could not be investigated at all -- even though
the same name sits in the log index and in the Wazuh agent list. The name is
what the three sources share; the Zabbix id is one source's handle for it.

Zabbix is still asked first, because that is where a monitored host almost
always is and asking costs no model call. The model is the fallback, and it only
sees the names the cheap path failed on.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import make_state

from aiops_rca.graph.deterministic_nodes import MAX_HOST_SEARCH_TURNS, ResolveHostsNode
from aiops_rca.schemas.investigation import InvestigationLimits, ResolvedHost
from aiops_rca.services.model_contracts import DiscoveredHost, HostSearchDecision
from aiops_rca.tools.result import ToolExecutionResult


class ZabbixThatKnowsNothing:
    """find_hosts answers, and matches nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, tool_name: str, arguments: Any, _context: Any) -> Any:
        self.calls.append((tool_name, dict(arguments)))
        return _ok("find_hosts", {"hosts": []})


def _ok(tool_name: str, response: Any, status: str = "ok") -> ToolExecutionResult:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    return ToolExecutionResult(
        tool_call_id=f"call-{tool_name}-{id(response)}",
        tool_name=tool_name,
        source="zabbix",
        status=status,
        request={},
        response=response,
        started_at=now,
        finished_at=now,
    )


class ScriptedModel:
    def __init__(self, *decisions: HostSearchDecision) -> None:
        self.decisions = list(decisions)
        self.payloads: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> HostSearchDecision:
        self.payloads.append(kwargs["payload"])
        return self.decisions[min(len(self.payloads) - 1, len(self.decisions) - 1)]


class RecordingExecutor:
    def __init__(self, response: Any = None) -> None:
        self.calls: list[Any] = []
        self.response = response if response is not None else {"hits": []}

    async def execute(self, planned: Any, _context: Any) -> ToolExecutionResult:
        self.calls.append(planned)
        return _ok(planned.tool_name, self.response)


def _state(**updates: Any):
    return make_state(
        host_queries=["ghost-host"],
        limits=InvestigationLimits(max_tool_calls=10),
        tool_catalog=[{"name": "search", "source": "elasticsearch"}],
        **updates,
    )


def _run(model=None, executor=None, zabbix=None):
    zabbix = zabbix or ZabbixThatKnowsNothing()
    node = ResolveHostsNode(zabbix, model=model, model_name="stub", executor=executor)
    return dict(asyncio.run(node(_state()))), zabbix


FOUND = HostSearchDecision(
    hosts=[DiscoveredHost(host="ghost-host", host_id=None, found_by="search")],
    tool_name=None,
    arguments_json="{}",
    stop_reason="로그 색인에서 찾음",
)

LOOK_IN_LOGS = HostSearchDecision(
    hosts=[],
    tool_name="search",
    arguments_json='{"index": "logs-*", "query_body": {"size": 1}}',
    stop_reason=None,
)


class TestWithoutTheFallback:
    def test_nothing_changes_when_no_model_is_wired(self):
        # The node is constructed without a model in tests that predate the
        # search, and in any deployment that has not enabled it.
        update, zabbix = _run()
        assert [name for name, _ in zabbix.calls] == ["find_hosts"]
        assert update["hosts"] == []
        assert update["unresolved_hosts"] == ["ghost-host"]
        assert update["stop_reason"] == "no host could be resolved for investigation"


class TestSearchingElsewhere:
    def test_zabbix_is_asked_first_and_only_once(self):
        # The fallback costs a model call. A monitored host must not pay for it.
        model = ScriptedModel(FOUND)
        _update, zabbix = _run(model=model, executor=RecordingExecutor())
        assert [name for name, _ in zabbix.calls] == ["find_hosts"]

    def test_a_host_found_elsewhere_carries_no_zabbix_id(self):
        # A log line does not have one, and inventing it would put a string into
        # every later Zabbix call that Zabbix has never heard of.
        model = ScriptedModel(FOUND)
        update, _ = _run(model=model, executor=RecordingExecutor())
        host = update["hosts"][0]
        assert isinstance(host, ResolvedHost)
        assert host.host == "ghost-host"
        assert host.host_id is None
        assert host.found_by == "search"

    def test_finding_it_clears_the_unresolved_name(self):
        model = ScriptedModel(FOUND)
        update, _ = _run(model=model, executor=RecordingExecutor())
        assert update["unresolved_hosts"] == []
        assert update["stop_reason"] != "no host could be resolved for investigation"

    def test_the_model_only_hears_about_names_zabbix_missed(self):
        model = ScriptedModel(FOUND)
        _run(model=model, executor=RecordingExecutor())
        assert model.payloads[0]["unresolved"] == ["ghost-host"]

    def test_a_named_tool_is_executed_and_its_answer_shown_back(self):
        model = ScriptedModel(LOOK_IN_LOGS, FOUND)
        executor = RecordingExecutor({"hits": [{"host": {"name": "ghost-host"}}]})
        update, _ = _run(model=model, executor=executor)
        assert [planned.tool_name for planned in executor.calls] == ["search"]
        # The second turn is given what the first turn's call returned, or it
        # has no way to read a name out of it.
        assert model.payloads[1]["attempts"][0]["tool_name"] == "search"
        assert "ghost-host" in model.payloads[1]["attempts"][0]["response"]
        assert update["hosts"][0].host == "ghost-host"

    def test_the_search_call_is_counted_against_the_budget(self):
        model = ScriptedModel(LOOK_IN_LOGS, FOUND)
        executor = RecordingExecutor()
        update, _ = _run(model=model, executor=executor)
        assert update["tool_call_count"] == 2

    def test_it_gives_up_rather_than_looping(self):
        model = ScriptedModel(LOOK_IN_LOGS)
        executor = RecordingExecutor()
        _run(model=model, executor=executor)
        assert len(executor.calls) == MAX_HOST_SEARCH_TURNS

    def test_unreadable_arguments_stop_the_search_and_say_so(self):
        broken = HostSearchDecision(
            hosts=[], tool_name="search", arguments_json="not json", stop_reason=None
        )
        model = ScriptedModel(broken)
        executor = RecordingExecutor()
        update, _ = _run(model=model, executor=executor)
        assert executor.calls == []
        assert "host_search_unusable" in [item.code for item in update["unknowns"]]


@pytest.mark.parametrize("budget", [1])
def test_the_search_is_skipped_when_the_budget_is_already_gone(budget):
    # find_hosts has already spent the only call. Asking a model where else to
    # look would produce a plan nothing can execute.
    model = ScriptedModel(LOOK_IN_LOGS)
    executor = RecordingExecutor()
    node = ResolveHostsNode(
        ZabbixThatKnowsNothing(), model=model, model_name="stub", executor=executor
    )
    state = make_state(
        host_queries=["ghost-host"],
        limits=InvestigationLimits(max_tool_calls=budget),
        tool_catalog=[],
    )
    update = dict(asyncio.run(node(state)))
    assert executor.calls == []
    assert "host_search_budget_exhausted" in [i.code for i in update["unknowns"]]
