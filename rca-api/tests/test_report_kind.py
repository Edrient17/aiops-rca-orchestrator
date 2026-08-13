"""Which report kind an investigation runs as, and what happens when it is wrong.

Asked "어제 vm-java-docker-2에서 14시 넘어서 문제 있었는지" with a two-row
catalog in front of it, the analyzer answered "incident_inquiry" -- a fair name
for the question and not one of the two on offer. select_template fell back to
incident_rca, which happened to be right, and said nothing. A misclassification
and a correct classification produced identical output.
"""

import pytest

from aiops_rca.api.models import ReportTemplate
from aiops_rca.schemas.parsed_request import ParsedRequest
from aiops_rca.services.investigation import _analyzer_output_type
from aiops_rca.services.templates import select_template

CATALOG = [
    ReportTemplate(
        template_id="incident_rca",
        version=1,
        title="장애 RCA 보고서",
        description="사건 하나를 다룰 때",
        enabled=True,
        collection={"window": {"range": "anchor_relative"}},
        output={},
    ),
    ReportTemplate(
        template_id="monthly_capacity_report",
        version=1,
        title="월말 용량 보고서",
        description="한 달간의 전반 상태",
        enabled=True,
        collection={"window": {"range": "last_calendar_month"}},
        output={},
    ),
]

BASE = {
    "schema_version": "0.1.0",
    "request_id": "R",
    "parse_status": "ready",
    "host_queries": ["vm-java-docker-2"],
    "anchor_time": "2026-08-12T14:00:00+09:00",
    "timezone": "Asia/Seoul",
    "incident_description": "어제 14시 이후 확인",
    "incident_type_hint": None,
    "user_intent": "확인",
    "initial_window_hint": None,
    "allow_dynamic_expansion": True,
    "ambiguities": [],
    "original_question": "어제 14시 넘어서 문제 있었어?",
}


def test_the_model_cannot_name_a_kind_outside_the_catalog():
    bound = _analyzer_output_type(("incident_rca", "monthly_capacity_report"))
    assert bound.model_validate({**BASE, "request_type": "incident_rca"})
    with pytest.raises(Exception, match="incident_rca"):
        bound.model_validate({**BASE, "request_type": "incident_inquiry"})


def test_an_empty_catalog_leaves_the_contract_open():
    # Nothing to constrain against, and refusing every id would strand the
    # request rather than let the fallback do its job.
    assert _analyzer_output_type(()) is ParsedRequest


def test_the_stored_contract_stays_permissive():
    # The schema keeps request_type a free string on purpose: report kinds are
    # rows an operator adds, and a build that has not seen a new one should
    # still be able to read a request that names it.
    assert ParsedRequest.model_validate({**BASE, "request_type": "some_new_kind"})


def test_a_matching_kind_is_selected_without_comment():
    template, unknown = select_template("monthly_capacity_report", CATALOG)
    assert template.template_id == "monthly_capacity_report"
    assert unknown is None


def test_a_fallback_is_recorded_rather_than_silent():
    template, unknown = select_template("incident_inquiry", CATALOG)
    assert template.template_id == "incident_rca"
    assert unknown is not None
    assert unknown.code == "report_kind_not_in_catalog"
    assert "incident_inquiry" in unknown.message


def test_a_catalog_without_the_fallback_is_a_configuration_error():
    with pytest.raises(ValueError, match="incident_rca"):
        select_template("anything", [CATALOG[1]])
