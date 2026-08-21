"""Finding the hosts an investigation is about.

This called `find_hosts` and nothing else, so a machine outside Zabbix could not
be investigated at all -- even though the same name sits in a log index and in a
Wazuh agent list. Keeping Zabbix as a fast path kept one tool's name in the
pipeline for the sake of a model call, which is the trade this project decided
against.

The node plans its lookups now. What survives from the old one is the property
worth keeping: a name that matches several hosts is left unresolved rather than
guessed at, because an investigation of the wrong machine reads exactly like an
investigation of the right one.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from conftest import make_state

from aiops_rca.graph.deterministic_nodes import MAX_HOST_SEARCH_TURNS, ResolveHostsNode
from aiops_rca.schemas.investigation import InvestigationLimits, ResolvedHost
from aiops_rca.services.model_contracts import DiscoveredHost, HostSearchDecision
from aiops_rca.tools.registry import ToolPolicyError
from aiops_rca.tools.result import ToolExecutionResult


def _result(tool_name: str, response: Any, status: str = "ok") -> ToolExecutionResult:
    now = datetime(2026, 8, 21, tzinfo=UTC)
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
    def __init__(self, response: Any = None, refuse: bool = False) -> None:
        self.calls: list[Any] = []
        self.response = response if response is not None else {"hosts": []}
        self.refuse = refuse

    async def execute(self, planned: Any, _context: Any) -> ToolExecutionResult:
        self.calls.append(planned)
        if self.refuse:
            raise ToolPolicyError(f"{planned.tool_name} is generic and needs the gate")
        return _result(planned.tool_name, self.response)


def _look(tool_name: str = "find_hosts", **arguments: Any) -> HostSearchDecision:
    return HostSearchDecision(
        hosts=[],
        tool_name=tool_name,
        arguments_json=json.dumps(arguments),
        stop_reason=None,
    )


def _report(*hosts: DiscoveredHost, stop: str = "찾음") -> HostSearchDecision:
    return HostSearchDecision(
        hosts=list(hosts), tool_name=None, arguments_json="{}", stop_reason=stop,
    )


def _found(host: str, host_id: str | None = None, by: str = "find_hosts"):
    return DiscoveredHost(host=host, host_id=host_id, found_by=by)


def _run(*decisions, queries=("vm-java-docker-2",), executor=None, **updates):
    model = ScriptedModel(*decisions)
    executor = executor or RecordingExecutor()
    node = ResolveHostsNode(model=model, model_name="stub", executor=executor)
    state = make_state(
        host_queries=list(queries),
        limits=updates.pop("limits", InvestigationLimits(max_tool_calls=10)),
        tool_catalog=[{"name": "find_hosts"}, {"name": "search"}],
        **updates,
    )
    return dict(asyncio.run(node(state))), model, executor


class TestResolvingByName:
    def test_one_match_resolves(self):
        update, _model, _executor = _run(
            _look(query="vm-java-docker-2"),
            _report(_found("vm-java-docker-2", "11094")),
        )
        assert [(h.host, h.host_id) for h in update["hosts"]] == [
            ("vm-java-docker-2", "11094"),
        ]
        assert update["unresolved_hosts"] == []
        assert update["stop_reason"] is None

    def test_several_matches_are_never_guessed_between(self):
        # An investigation of the wrong machine reads exactly like one of the
        # right machine, so the name is left unresolved and said so.
        update, _model, _executor = _run(
            _look(query="payment"),
            _report(_found("payment-api", "1"), _found("payment-worker", "2")),
            queries=("payment",),
        )
        assert update["hosts"] == []
        assert update["unresolved_hosts"] == ["payment"]
        assert [i.code for i in update["unknowns"]] == ["host_ambiguous"]
        assert update["stop_reason"] == "no host could be resolved for investigation"

    def test_a_name_nothing_matched_says_so(self):
        update, _model, _executor = _run(_report(stop="아무것도 못 찾음"))
        assert update["unresolved_hosts"] == ["vm-java-docker-2"]
        assert [i.code for i in update["unknowns"]] == ["host_not_found"]

    def test_a_host_found_outside_zabbix_carries_no_id(self):
        # A log line does not have one, and inventing it would put a string into
        # every later Zabbix call that Zabbix has never heard of.
        update, _model, _executor = _run(
            _look("search", index="vm-logs-*"),
            _report(_found("vm-java-docker-2", None, by="search")),
        )
        host = update["hosts"][0]
        assert isinstance(host, ResolvedHost)
        assert host.host_id is None
        assert host.found_by == "search"


class TestChoosingWhereToLook:
    def test_the_named_tool_is_what_gets_called(self):
        # Nothing here names a tool. The catalog is offered and the model picks.
        _update, _model, executor = _run(
            _look("search", index="vm-logs-*"),
            _report(_found("vm-java-docker-2", None, by="search")),
        )
        assert [call.tool_name for call in executor.calls] == ["search"]

    def test_the_selector_is_passed_through_as_data(self):
        # A host group is the template's idea, not this node's. It is handed to
        # the model rather than turned into arguments here.
        selector = {"mode": "host_group", "group_ids": ["73"]}
        _update, model, _executor = _run(
            _report(_found("in-the-group", "10")),
            collection={"host_selector": selector},
        )
        assert model.payloads[0]["host_selector"] == selector

    def test_a_host_the_request_did_not_name_is_still_kept(self):
        # Listing a group returns hosts nobody asked for by name; they are the
        # answer, not a mismatch.
        update, _model, _executor = _run(
            _report(_found("in-the-group", "10")),
            queries=(),
            collection={"host_selector": {"mode": "host_group", "group_ids": ["73"]}},
        )
        assert [h.host for h in update["hosts"]] == ["in-the-group"]

    def test_the_last_lookup_is_read_before_giving_up(self):
        # Live, the host was in Wazuh and the first lookup went to the log
        # index. The model named Wazuh on its second turn, the call was made,
        # and the loop ended before anyone read the answer.
        _update, model, executor = _run(
            _look("find_hosts", query="x"),
            _look("search", index="vm-logs-*"),
            _report(_found("vm-java-docker-2", None, by="search")),
        )
        assert len(executor.calls) == MAX_HOST_SEARCH_TURNS
        assert len(model.payloads) == MAX_HOST_SEARCH_TURNS + 1

    def test_it_gives_up_rather_than_looping(self):
        _update, _model, executor = _run(_look(query="x"))
        assert len(executor.calls) == MAX_HOST_SEARCH_TURNS


class TestWhenTheLookupCannotHappen:
    def test_the_budget_stops_it_cleanly(self):
        update, _model, executor = _run(
            _look(query="x"),
            limits=InvestigationLimits(max_tool_calls=1),
            tool_call_count=1,
            tool_results=[_result("earlier", {})],
        )
        assert executor.calls == []
        assert "host_search_budget_exhausted" in [i.code for i in update["unknowns"]]

    def test_unreadable_arguments_stop_the_search_and_say_so(self):
        broken = HostSearchDecision(
            hosts=[], tool_name="search", arguments_json="not json", stop_reason=None,
        )
        update, _model, executor = _run(broken)
        assert executor.calls == []
        assert "host_search_unusable" in [i.code for i in update["unknowns"]]

    def test_a_refused_call_is_recorded_rather_than_raised(self):
        update, _model, _executor = _run(
            _look("search", index="vm-logs-*"),
            executor=RecordingExecutor(refuse=True),
        )
        assert "host_search_blocked" in [i.code for i in update["unknowns"]]
        assert update["stop_reason"] == "no host could be resolved for investigation"
