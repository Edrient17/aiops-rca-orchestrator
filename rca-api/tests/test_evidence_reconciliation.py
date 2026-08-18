"""An event that was collected cannot leave the report unmentioned.

A `/etc/passwd has been changed` event was observed, reached the phenomenon
summary, and then appeared nowhere in the finished report. The availability
section counted the three outages and left it out as unrelated to uptime, and
no other section owned it. Nothing was wrong with any single decision; the
event simply fell between them.
"""

from datetime import UTC, datetime

from aiops_rca.schemas.report import Report, ReportItem, ReportSection
from aiops_rca.services.investigation import _reconcile_evidence, _writer_sections

OUTPUT = {
    "sections": [
        {"id": "availability", "heading": "가용성", "required": True,
         "requires_effects": ["incident_events"]},
        {"id": "capacity_trend", "heading": "용량 추세", "required": True,
         "requires_effects": ["metric_change"]},
        {"id": "limitations", "heading": "한계", "required": True},
    ],
}


def _evidence(evidence_id: str, evidence_type: str, summary: str):
    return {
        "evidence_id": evidence_id,
        "evidence_type": evidence_type,
        "source": "zabbix",
        "summary": summary,
        "observed_at": datetime(2026, 7, 16, 2, 28, tzinfo=UTC),
        "window": None,
        "resource_ids": {
            "host_id": "10663",
            "event_id": "24043679",
            "trigger_id": None,
            "item_id": None,
        },
        "metric": None,
        "data_quality": None,
        "tool_call_id": "call-1",
        "search_query": None,
    }


class Package:
    def __init__(self, evidence, unknowns=()):
        from aiops_rca.schemas.evidence_package import Evidence

        self.evidence = [Evidence.model_validate(item) for item in evidence]
        self.unknowns = list(unknowns)


OUTAGE = _evidence("zbx:event:23835785", "event", "host is unreachable")
PASSWD = _evidence("zbx:event:24043679", "event", "/etc/passwd has been changed")
METRIC = _evidence("zbx:metric:120124:1-2-1d", "metric_summary", "CPU 사용률")


def _report(cited: list[str]) -> Report:
    return Report(
        title="2026년 7월 월말 보고서",
        sections=[
            ReportSection(
                id="availability",
                items=[
                    ReportItem(
                        text="비가용 3건이 관측되었습니다.",
                        label="midibus",
                        evidence_refs=cited,
                        counter_evidence_refs=[],
                    ),
                ],
            ),
            ReportSection(id="limitations", body="조사 범위는 7월입니다.", items=[]),
        ],
    )


def test_an_uncited_event_lands_in_limitations():
    package = Package([OUTAGE, PASSWD])
    report = _reconcile_evidence(_report(["zbx:event:23835785"]), OUTPUT, package)

    limitations = next(s for s in report.sections if s.id == "limitations")
    refs = [ref for item in limitations.items for ref in item.evidence_refs]
    assert "zbx:event:24043679" in refs
    assert "/etc/passwd" in limitations.items[0].text
    # The section's own prose survives; the append is additive.
    assert limitations.body == "조사 범위는 7월입니다."


def test_a_cited_event_is_left_alone():
    package = Package([OUTAGE, PASSWD])
    report = _reconcile_evidence(
        _report(["zbx:event:23835785", "zbx:event:24043679"]), OUTPUT, package
    )
    limitations = next(s for s in report.sections if s.id == "limitations")
    assert limitations.items == []


def test_metrics_are_not_expected_to_be_cited_one_by_one():
    # A metric series is summarized in aggregate. Listing every uncited one
    # would bury the events this check exists to surface.
    package = Package([OUTAGE, METRIC])
    report = _reconcile_evidence(_report(["zbx:event:23835785"]), OUTPUT, package)
    limitations = next(s for s in report.sections if s.id == "limitations")
    assert limitations.items == []


def test_a_template_without_a_limitations_section_is_not_given_one():
    # The append has to land somewhere the template actually declares, or the
    # report would gain a section the writer contract forbids.
    package = Package([OUTAGE, PASSWD])
    output = {"sections": [{"id": "availability"}]}
    report = _reconcile_evidence(_report(["zbx:event:23835785"]), output, package)
    limitations = next(s for s in report.sections if s.id == "limitations")
    assert limitations.items == []


class TestTheWriterIsToldWhatIsMissing:
    def test_a_section_whose_evidence_never_arrived_is_marked(self):
        from aiops_rca.schemas.investigation import UnknownItem

        package = Package(
            [OUTAGE],
            [
                UnknownItem(
                    code="declared_effect_uncovered",
                    message=(
                        "The report declares sections built from observations"
                        " this investigation could not collect: metric_change"
                    ),
                ),
            ],
        )
        sections = {item["id"]: item for item in _writer_sections(OUTPUT, package)}
        assert sections["capacity_trend"]["evidence_unavailable"] == ["metric_change"]
        # The section that did get its evidence carries no such marker.
        assert "evidence_unavailable" not in sections["availability"]

    def test_nothing_is_marked_when_everything_was_collected(self):
        package = Package([OUTAGE])
        sections = _writer_sections(OUTPUT, package)
        assert all("evidence_unavailable" not in item for item in sections)
