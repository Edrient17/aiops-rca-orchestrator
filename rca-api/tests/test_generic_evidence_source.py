"""Generic tool output is filed under the source the registry assigned it.

`list_relevant_metrics` is a Zabbix tool with no dedicated normalizer, so it
fell through a two-way branch that filed anything non-Wazuh as an Elasticsearch
log line. A monthly report then cited `log:lines:vm-java-docker-2:...` as the
evidence for a statement about which Zabbix items exist.
"""

from datetime import UTC, datetime

import pytest

from aiops_rca.schemas.investigation import PlannedToolCall
from aiops_rca.tools.normalizer import normalize_observation
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY
from aiops_rca.tools.result import ToolExecutionResult


def _evidence(tool_name: str, source: str):
    result = ToolExecutionResult(
        tool_call_id="call-1",
        tool_name=tool_name,
        source=source,
        status="ok",
        request={"host_id": "11094"},
        response={"metrics": [{"item_id": "120124"}]},
        started_at=datetime(2026, 8, 18, tzinfo=UTC),
        finished_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    planned = PlannedToolCall(
        tool_name=tool_name,
        arguments={"host_id": "11094"},
        purpose="어떤 numeric item이 있는가",
        target_hypothesis_ids=[],
        host_id="11094",
    )
    return normalize_observation(
        result, planned, host_id="11094", host="vm-java-docker-2"
    )[0]


def test_a_zabbix_tool_is_filed_as_zabbix():
    evidence = _evidence("list_relevant_metrics", "zabbix")
    assert evidence.source == "zabbix"
    assert evidence.evidence_id.startswith("zbx:object:")


def test_wazuh_and_elasticsearch_keep_their_shapes():
    assert _evidence("get_wazuh_agents", "wazuh").evidence_id.startswith("wazuh:alerts:")
    assert _evidence("search", "elasticsearch").evidence_id.startswith("log:lines:")


@pytest.mark.parametrize(
    "policy",
    [p for p in DEFAULT_TOOL_REGISTRY.list()],
    ids=lambda p: p.name,
)
def test_every_allowlisted_tool_has_a_shape_for_its_source(policy):
    # A source with no entry would raise KeyError at normalization time, which
    # is the middle of an investigation -- the worst place to learn it.
    from aiops_rca.tools.normalizer import _GENERIC_SHAPE

    assert policy.source in _GENERIC_SHAPE
