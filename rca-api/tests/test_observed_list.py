"""A list of rows needs somewhere to go that is not a prose field.

Evidence had a typed slot for a metric and none for a list, so anything
returning rows went through the 3000-character summary and was cut there. A
host running sixty services reported fifteen, and nothing said which fifteen.

Trimming each tool's fields to fit would be the same work again for every tool
added, and forgetting it degrades a report quietly. The container is what was
wrong.
"""

from datetime import UTC, datetime

import pytest

from aiops_rca.schemas.investigation import PlannedToolCall
from aiops_rca.tools.normalizer import LIST_CAPACITY_CHARS, normalize_observation
from aiops_rca.tools.result import ToolExecutionResult


def _evidence(tool_name: str, source: str, response):
    result = ToolExecutionResult(
        tool_call_id="call-1",
        tool_name=tool_name,
        source=source,
        status="ok",
        request={"agent_id": "001"},
        response=response,
        started_at=datetime(2026, 8, 19, tzinfo=UTC),
        finished_at=datetime(2026, 8, 19, tzinfo=UTC),
    )
    planned = PlannedToolCall(
        tool_name=tool_name,
        arguments={"agent_id": "001"},
        purpose="현재 상태",
        target_hypothesis_ids=[],
        host_id="11094",
    )
    return normalize_observation(result, planned, host_id="11094", host="vm-a")[0]


def _processes(count: int):
    return {
        "agent_id": "001",
        "processes": [
            {
                "pid": 1000 + i,
                "name": f"service-{i}",
                "user": "root",
                "command": f"/usr/bin/service-{i} --flag",
            }
            for i in range(count)
        ],
        "returned": count,
        "partial": False,
    }


def test_a_whole_host_of_services_fits():
    # Sixty services is a real host. The old path carried fifteen of them.
    evidence = _evidence("get_wazuh_agent_processes", "wazuh", _processes(60))
    assert evidence.observed is not None
    assert len(evidence.observed.items) == 60
    assert evidence.observed.omitted == 0


def test_the_summary_describes_rather_than_carries():
    evidence = _evidence("get_wazuh_agent_processes", "wazuh", _processes(60))
    assert "60 processes" in evidence.summary
    assert "service-59" not in evidence.summary


def test_what_the_tool_said_besides_the_rows_survives():
    # returned, limits and the tool's own partial flag are the difference
    # between a short answer and a cut one, so they stay in the sentence.
    evidence = _evidence("get_wazuh_agent_processes", "wazuh", _processes(3))
    assert "partial" in evidence.summary
    assert "returned" in evidence.summary


class TestWhenEvenTheSlotIsNotEnough:
    def test_the_list_is_trimmed_and_says_by_how_many(self):
        evidence = _evidence("get_wazuh_agent_processes", "wazuh", _processes(2000))
        assert evidence.observed.omitted > 0
        carried = len(evidence.observed.items)
        assert carried + evidence.observed.omitted == 2000

    def test_the_trim_respects_the_budget(self):
        import json

        evidence = _evidence("get_wazuh_agent_processes", "wazuh", _processes(2000))
        size = len(json.dumps(evidence.observed.items, ensure_ascii=False))
        # One row may cross the line; the budget bounds the rest.
        assert size < LIST_CAPACITY_CHARS * 1.2

    def test_at_least_one_row_is_always_carried(self):
        # A single row larger than the budget still travels: an empty list with
        # "2000 omitted" tells a reader nothing they can act on.
        huge = {"processes": [{"name": "x" * (LIST_CAPACITY_CHARS * 2)}]}
        evidence = _evidence("get_wazuh_agent_processes", "wazuh", huge)
        assert len(evidence.observed.items) == 1


class TestWhichFieldHoldsTheList:
    def test_it_comes_from_the_registry(self):
        # No per-tool knowledge here: result_list_fields already declares it,
        # and the result classifier already reads the same declaration.
        evidence = _evidence(
            "get_wazuh_agent_ports",
            "wazuh",
            {"ports": [{"local_port": 22, "process": "sshd"}], "returned": 1},
        )
        assert evidence.observed.kind == "ports"

    def test_an_observation_that_is_not_a_list_gets_no_slot(self):
        evidence = _evidence("get_wazuh_agents", "wazuh", {"agents": []})
        assert evidence.observed is None

    @pytest.mark.parametrize("response", ["text", None, 42, {"unrelated": 1}])
    def test_an_unlisted_shape_falls_back_to_the_old_summary(self, response):
        evidence = _evidence("get_wazuh_agent_processes", "wazuh", response)
        assert evidence.observed is None
        assert evidence.summary
