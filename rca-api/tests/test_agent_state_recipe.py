"""Current host state, collected without a model choosing to ask.

Asked what was running on a host right now, the investigation opened with a
Zabbix event scan, framed the whole question around Zabbix's visibility, and
spent three calls searching Zabbix for process items that do not exist. The
Wazuh tools that answer the question were in the catalog the whole time.

A section that declares `current_process_state` gets it collected regardless of
where the reasoning goes. The two steps exist because those tools key on a
Wazuh agent id and an investigation knows a Zabbix host.
"""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from conftest import make_state

from aiops_rca.graph.coverage_nodes import CoverageSweepNode
from aiops_rca.schemas.investigation import ResolvedHost
from aiops_rca.tools.adapters.base import AdapterSet, McpAdapter
from aiops_rca.tools.coverage import _agent_id, service_processes
from aiops_rca.tools.executor import ToolExecutor
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY

# What the deployment returns now that the tools answer with structured
# content. They used to answer in prose, and the id was read out of
# "Agent ID:"/"Name:" lines -- which is why the fork was changed.
AGENT_LIST = {
    "agents": [
        {"id": "000", "name": "vm-wazuh-server", "status": "active", "is_manager": True},
        {"id": "001", "name": "vm-java-docker-2", "status": "active", "is_manager": False},
        {"id": "002", "name": "test-java-docker-vm", "status": "disconnected", "is_manager": False},
    ],
    "returned": 3,
}

PROCESSES = {
    "agent_id": "001",
    "processes": [
        {"pid": 10, "name": "mm_percpu_wq", "ppid": "2", "kernel_thread": True},
        {"pid": 105, "name": "kswapd0", "ppid": "2", "kernel_thread": True},
        {"pid": 702, "name": "sshd", "ppid": "1", "kernel_thread": False},
        {"pid": 1841, "name": "java", "ppid": "1502", "kernel_thread": False},
    ],
    "returned": 4,
    "limit": 500,
    "partial": False,
}

PORTS = {
    "agent_id": "001",
    "ports": [
        {"protocol": "tcp", "local_ip": "0.0.0.0", "local_port": 22,
         "state": "listening", "process": "sshd", "externally_bound": True},
        {"protocol": "tcp", "local_ip": "127.0.0.53", "local_port": 53,
         "state": "listening", "process": "systemd-resolve", "externally_bound": False},
    ],
    "returned": 2,
    "limit": 500,
    "partial": False,
}


class TestReadingTheAgentId:
    def test_it_finds_the_host_it_was_asked_about(self):
        assert _agent_id(AGENT_LIST, "vm-java-docker-2") == "001"
        assert _agent_id(AGENT_LIST, "test-java-docker-vm") == "002"

    def test_the_manager_is_addressable_like_any_other(self):
        assert _agent_id(AGENT_LIST, "vm-wazuh-server") == "000"

    def test_an_absent_host_returns_nothing_rather_than_a_neighbour(self):
        # Returning the wrong id would read another machine's processes and
        # look entirely plausible doing it.
        assert _agent_id(AGENT_LIST, "vm-not-registered") is None

    def test_a_name_that_is_a_prefix_of_another_does_not_match(self):
        assert _agent_id(AGENT_LIST, "vm-java") is None

    @pytest.mark.parametrize(
        "response",
        [None, 42, {}, "", {"agents": []}, "Agent ID: 001\nName: vm-java-docker-2"],
    )
    def test_an_unusable_response_returns_nothing(self, response):
        # The prose form is listed deliberately: the tools no longer answer
        # that way, and reading it again would mean the fork had regressed.
        assert _agent_id(response, "vm-java-docker-2") is None


class TestChoosingWhichProcessesToReport:
    def test_kernel_threads_are_left_out(self):
        # A host runs a few dozen services and a hundred-odd kernel threads,
        # and the threads hold the low PIDs. A limit of fifty returned
        # forty-nine of them and nothing else -- the answer was past the cut.
        assert [item["name"] for item in service_processes(PROCESSES)] == ["sshd", "java"]

    @pytest.mark.parametrize("response", [None, "text", {}, {"processes": []}])
    def test_an_unusable_response_yields_nothing(self, response):
        assert service_processes(response) == []



class ScriptedTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((tool_name, dict(arguments)))
        if tool_name == "get_wazuh_agents":
            return AGENT_LIST
        if tool_name == "get_wazuh_agent_processes":
            return PROCESSES
        if tool_name == "get_wazuh_agent_ports":
            return PORTS
        return {}


def _sweep(effects, hosts=None, **updates):
    transport = ScriptedTransport()
    adapter = McpAdapter(
        source="wazuh",
        registry=DEFAULT_TOOL_REGISTRY,
        transport=transport,
        timeout_seconds=1,
    )
    executor = ToolExecutor(
        AdapterSet({"zabbix": adapter, "elasticsearch": adapter, "wazuh": adapter}),
        DEFAULT_TOOL_REGISTRY,
    )
    state = make_state(
        hosts=hosts or [ResolvedHost(host="vm-java-docker-2", host_id="11094")],
        collection={
            "resolved_window": {
                "from": "2026-08-19T02:37:14Z",
                "to": "2026-08-19T02:47:14Z",
            },
            "required_effects": effects,
        },
        **updates,
    )
    update = asyncio.run(CoverageSweepNode(executor, DEFAULT_TOOL_REGISTRY)(state))
    return update, transport


def test_only_what_was_declared_is_collected():
    # The recipe covers two effects. Running both regardless meant a template
    # asking only about processes still paid for a port query on every host.
    update, transport = _sweep(["current_process_state"])
    assert [name for name, _ in transport.calls] == [
        "get_wazuh_agents",
        "get_wazuh_agent_processes",
    ]
    assert update["tool_call_count"] == 2


def test_declaring_both_collects_both():
    _, transport = _sweep(["current_process_state", "current_port_state"])
    assert [name for name, _ in transport.calls] == [
        "get_wazuh_agents",
        "get_wazuh_agent_processes",
        "get_wazuh_agent_ports",
    ]


def test_ports_alone_skips_the_process_read():
    _, transport = _sweep(["current_port_state"])
    assert [name for name, _ in transport.calls] == [
        "get_wazuh_agents",
        "get_wazuh_agent_ports",
    ]


def test_the_resolved_agent_id_is_the_one_used():
    _, transport = _sweep(["current_port_state"])
    _, arguments = next(c for c in transport.calls if c[0] == "get_wazuh_agent_ports")
    assert arguments["agent_id"] == "001"


def test_the_port_query_asks_for_what_is_listening():
    _, transport = _sweep(["current_port_state"])
    _, arguments = next(c for c in transport.calls if c[0] == "get_wazuh_agent_ports")
    # The tool's enum: lower case protocol, upper case state. Either spelled
    # the other way is refused before the call reaches Wazuh.
    assert arguments["protocol"] == "tcp"
    assert arguments["state"] == "LISTENING"


def test_a_host_with_no_agent_is_skipped_without_guessing():
    update, transport = _sweep(
        ["current_process_state"],
        hosts=[ResolvedHost(host="vm-not-registered", host_id="99999")],
    )
    # The lookup happens, and nothing follows it.
    assert [name for name, _ in transport.calls] == ["get_wazuh_agents"]
    codes = [item.code for item in update["unknowns"]]
    assert "declared_effect_uncovered" in codes


def test_kernel_threads_are_gone_from_the_evidence():
    # Not just filtered in a helper -- filtered on the way into the evidence,
    # which is the only place a report ever reads. The helper existed and was
    # tested for a day before anything called it, and the reports stayed full
    # of kernel threads that whole time.
    update, _ = _sweep(["current_process_state"])
    process_evidence = [
        item
        for item in update["evidence"]
        if "Read the current state" in (item.summary or "")
        and "processes" in (item.summary or "")
    ]
    assert process_evidence
    summary = process_evidence[0].summary
    assert "sshd" in summary
    assert "java" in summary
    assert "mm_percpu_wq" not in summary
    assert "kswapd0" not in summary


def test_the_omitted_count_is_kept():
    # A shorter list needs the reason for being shorter, or it reads as a host
    # with four processes.
    update, _ = _sweep(["current_process_state"])
    summary = next(
        item.summary
        for item in update["evidence"]
        if "kernel_threads_omitted" in (item.summary or "")
    )
    assert "kernel_threads_omitted" in summary


def test_enough_rows_are_asked_for_to_reach_past_the_threads():
    from aiops_rca.tools.coverage import AGENT_STATE_ROWS

    # Fifty returned forty-nine kernel threads on a real host. The limit has to
    # clear them before the services begin.
    assert AGENT_STATE_ROWS >= 300
    _, transport = _sweep(["current_process_state"])
    _, arguments = next(
        c for c in transport.calls if c[0] == "get_wazuh_agent_processes"
    )
    assert arguments["limit"] == AGENT_STATE_ROWS


def test_the_lookup_is_not_made_without_room_for_what_follows():
    # The agent lookup only earns its call if a read follows it. Asking for both
    # reads needs three calls; two leaves the id with nothing to use it.
    update, transport = _sweep(
        ["current_process_state", "current_port_state"],
        limits={"max_tool_calls": 2},
    )
    assert transport.calls == []
    assert "declared_effect_uncovered" in [i.code for i in update["unknowns"]]


def test_one_read_needs_only_two_calls():
    _, transport = _sweep(["current_process_state"], limits={"max_tool_calls": 2})
    assert [name for name, _ in transport.calls] == [
        "get_wazuh_agents",
        "get_wazuh_agent_processes",
    ]
