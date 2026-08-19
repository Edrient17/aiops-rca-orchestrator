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
from aiops_rca.tools.coverage import _agent_id
from aiops_rca.tools.executor import ToolExecutor
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY

# Exactly what the deployment returns, from mcp-server-wazuh's own format
# string: "Agent ID: {}\nName: {}\nStatus: {}...", one block per agent.
AGENT_LIST = (
    "Agent ID: 000 (Wazuh Manager)\nName: vm-wazuh-server\nStatus: 🟢 ACTIVE\n"
    "IP: 127.0.0.1\nOS: Ubuntu 22.04.5 LTS (x86_64)\n"
    "Agent ID: 001\nName: vm-java-docker-2\nStatus: 🟢 ACTIVE\n"
    "IP: 192.168.20.7\nGroups: default\n"
    "Agent ID: 002\nName: test-java-docker-vm\nStatus: 🔴 DISCONNECTED\n"
)


class TestReadingTheAgentId:
    def test_it_finds_the_host_it_was_asked_about(self):
        assert _agent_id(AGENT_LIST, "vm-java-docker-2") == "001"
        assert _agent_id(AGENT_LIST, "test-java-docker-vm") == "002"

    def test_the_manager_suffix_is_not_part_of_the_id(self):
        assert _agent_id(AGENT_LIST, "vm-wazuh-server") == "000"

    def test_an_absent_host_returns_nothing_rather_than_a_neighbour(self):
        # Returning the wrong id would read another machine's processes and
        # look entirely plausible doing it.
        assert _agent_id(AGENT_LIST, "vm-not-registered") is None

    @pytest.mark.parametrize("response", [None, 42, {}, "", "no agents found"])
    def test_an_unusable_response_returns_nothing(self, response):
        assert _agent_id(response, "vm-java-docker-2") is None

    def test_a_name_that_is_a_prefix_of_another_does_not_match(self):
        listing = "Agent ID: 001\nName: vm-java-docker-2\n"
        assert _agent_id(listing, "vm-java") is None


class ScriptedTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((tool_name, dict(arguments)))
        if tool_name == "get_wazuh_agents":
            return AGENT_LIST
        if tool_name == "get_wazuh_agent_processes":
            return "PID: 702\nName: sshd\nState: S\n"
        if tool_name == "get_wazuh_agent_ports":
            return "Protocol: tcp\nLocal: 0.0.0.0:22\nState: listening\nProcess Name: sshd\n"
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


def test_a_declared_process_section_is_collected():
    update, transport = _sweep(["current_process_state"])
    assert [name for name, _ in transport.calls] == [
        "get_wazuh_agents",
        "get_wazuh_agent_processes",
        "get_wazuh_agent_ports",
    ]
    assert update["tool_call_count"] == 3


def test_the_resolved_agent_id_is_the_one_used():
    _, transport = _sweep(["current_port_state"])
    _, arguments = next(c for c in transport.calls if c[0] == "get_wazuh_agent_processes")
    assert arguments["agent_id"] == "001"


def test_the_port_query_asks_for_what_is_listening():
    _, transport = _sweep(["current_port_state"])
    _, arguments = next(c for c in transport.calls if c[0] == "get_wazuh_agent_ports")
    assert arguments["protocol"] == "tcp"
    assert arguments["state"] == "listening"


def test_a_host_with_no_agent_is_skipped_without_guessing():
    update, transport = _sweep(
        ["current_process_state"],
        hosts=[ResolvedHost(host="vm-not-registered", host_id="99999")],
    )
    # The lookup happens, and nothing follows it.
    assert [name for name, _ in transport.calls] == ["get_wazuh_agents"]
    codes = [item.code for item in update["unknowns"]]
    assert "declared_effect_uncovered" in codes


def test_the_pair_is_not_started_without_room_to_finish():
    update, transport = _sweep(["current_process_state"], limits={"max_tool_calls": 2})
    assert transport.calls == []
    assert "declared_effect_uncovered" in [i.code for i in update["unknowns"]]
