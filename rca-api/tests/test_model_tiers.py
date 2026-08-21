"""Which model each stage of the investigation gets.

The five investigation nodes shared one setting, so reading a host name out of
a JSON response was billed at whatever weighing evidence against hypotheses
needed, and neither could be tuned without moving the other. The split is by
what a step decides: a step that judges aims the whole investigation, a step
that fetches a name fails loudly and gets another turn.
"""

import pytest

from aiops_rca.config.settings import Settings
from aiops_rca.services.investigation import (
    InvestigationService,
    _collector_models,
)
from aiops_rca.sources import SOURCES
from aiops_rca.tools.adapters.base import AdapterSet


def make_settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        aiops_internal_token="internal-secret",
        openai_api_key="test-openai-key",
        zabbix_mcp_url="http://zabbix-mcp/mcp",
        zabbix_mcp_auth_token="test-zabbix-token",
        oss_es_mcp_url="http://elasticsearch:8081/mcp",
        wazuh_mcp_url="http://wazuh-mcp/mcp",
        wazuh_mcp_auth_token="test-wazuh-token",
        **overrides,
    )


class TestTheDefaultTiers:
    def test_judging_and_fetching_do_not_get_the_same_model(self):
        settings = make_settings()
        assert settings.rca_reasoning_model != settings.rca_routine_model

    def test_the_report_the_operator_reads_gets_the_stronger_model(self):
        settings = make_settings()
        assert settings.rca_writer_model == settings.rca_reasoning_model


class TestTheAuditRow:
    def test_it_names_both_models_when_the_stage_ran_two(self):
        # One row, no longer one model. Naming only the stronger one would
        # understate what the cheaper node decided.
        row = _collector_models(
            make_settings(rca_reasoning_model="strong", rca_routine_model="cheap"),
        )
        assert row == "strong+cheap"

    def test_it_names_one_when_both_tiers_are_the_same_model(self):
        row = _collector_models(
            make_settings(rca_reasoning_model="same", rca_routine_model="same"),
        )
        assert row == "same"


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ("resolve_hosts", "cheap"),
        ("establish_phenomenon", "strong"),
        ("hypothesis_planner", "strong"),
        ("observation_planner", "strong"),
        ("hypothesis_updater", "strong"),
    ],
)
def test_each_node_is_wired_to_the_tier_its_work_belongs_to(node, expected):
    # Wired at construction, so a node quietly moving back onto one shared
    # setting is caught here rather than on the next invoice.
    service = InvestigationService(
        settings=make_settings(
            rca_reasoning_model="strong", rca_routine_model="cheap"
        ),
        model=object(),
        adapters=AdapterSet({source: object() for source in SOURCES}),
    )
    assert getattr(service.nodes, node).model_name == expected
