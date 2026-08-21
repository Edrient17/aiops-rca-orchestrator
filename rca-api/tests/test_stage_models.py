"""Which model each stage of the investigation runs on.

The five investigation nodes shared one setting, so reading a host name out of
a JSON response was billed at whatever weighing evidence against hypotheses
needed, and neither could be moved without moving the other. Each stage names
its own model now, falling back to one default when it has no opinion.
"""

import pytest

from aiops_rca.config.settings import Settings
from aiops_rca.services.investigation import (
    COLLECTOR_STAGES,
    InvestigationService,
    _collector_models,
)
from aiops_rca.sources import SOURCES
from aiops_rca.tools.adapters.base import AdapterSet

ALL_STAGES = (*COLLECTOR_STAGES, "question_analyzer", "report_writer")


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


def build(**overrides) -> InvestigationService:
    return InvestigationService(
        settings=make_settings(**overrides),
        model=object(),
        adapters=AdapterSet({source: object() for source in SOURCES}),
    )


class TestChoosingPerStage:
    @pytest.mark.parametrize("stage", ALL_STAGES)
    def test_every_stage_can_be_moved_on_its_own(self, stage):
        settings = make_settings(**{f"rca_model_{stage}": "chosen"})
        assert settings.model_for(stage) == "chosen"
        others = [other for other in ALL_STAGES if other != stage]
        assert all(settings.model_for(other) != "chosen" for other in others)

    @pytest.mark.parametrize("stage", ALL_STAGES)
    def test_a_stage_with_no_opinion_follows_the_default(self, stage):
        settings = make_settings(
            rca_model="fallback", **{f"rca_model_{name}": None for name in ALL_STAGES}
        )
        assert settings.model_for(stage) == "fallback"

    @pytest.mark.parametrize("stage", ALL_STAGES)
    def test_an_empty_setting_follows_the_default_too(self, stage):
        # docker compose expands an unset variable to "", not to nothing, so a
        # stage nobody chose arrives as an empty string rather than absent.
        settings = make_settings(
            rca_model="fallback", **{f"rca_model_{name}": "" for name in ALL_STAGES}
        )
        assert settings.model_for(stage) == "fallback"

    def test_a_stage_nobody_configured_stops_the_service(self):
        # The graph is built at startup, so a typo here has to fail there
        # rather than quietly downgrade one node for the life of the process.
        with pytest.raises(AttributeError):
            make_settings().model_for("no_such_stage")


class TestWhatShipsByDefault:
    @pytest.mark.parametrize("stage", ALL_STAGES)
    def test_every_stage_ships_on_the_default(self, stage):
        # The writer ran a tier above for a while, on the argument that it
        # produces what an operator reads. Measured, it was 7% of one
        # investigation's tokens and 59% of its bill. Raising a stage stays a
        # one-line change, so it can be tried again against a number.
        settings = make_settings()
        assert settings.model_for(stage) == settings.rca_model


class TestTheAuditRow:
    def test_it_names_every_distinct_model_the_stage_ran(self):
        # One row, no longer one model. Naming only the strongest would
        # understate what the cheaper nodes decided.
        row = _collector_models(
            make_settings(
                rca_model="cheap",
                rca_model_observation_planner="strong",
            ),
        )
        assert row == "cheap+strong"

    def test_it_names_one_when_every_stage_runs_the_same_model(self):
        row = _collector_models(
            make_settings(
                rca_model="same",
                **{f"rca_model_{stage}": None for stage in COLLECTOR_STAGES},
            ),
        )
        assert row == "same"


@pytest.mark.parametrize("stage", COLLECTOR_STAGES)
def test_each_node_is_built_with_the_model_its_stage_names(stage):
    # Which model a node was given disappears once the graph is compiled, so a
    # node sliding back onto a shared setting is caught here rather than on the
    # next invoice.
    service = build(**{f"rca_model_{stage}": "chosen", "rca_model": "default"})
    assert getattr(service.nodes, stage).model_name == "chosen"
