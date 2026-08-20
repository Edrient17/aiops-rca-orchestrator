"""The writer path must work against the package the graph actually builds.

A monthly question returned 500 with `'str' object has no attribute 'code'`.
_writer_sections read `package.unknowns` as UnknownItem objects; the builder
keeps only `item.message`, so they are strings. Every test passed, because the
test double held the richer type the real object never has.

These tests use EvidencePackage itself, so the contract cannot drift again.
"""

from datetime import UTC, datetime

from conftest import make_state

from aiops_rca.graph.live_nodes import EvidencePackageBuilderNode
from aiops_rca.schemas.evidence_package import Evidence, EvidencePackage
from aiops_rca.schemas.investigation import Hypothesis, ResolvedHost, UnknownItem
from aiops_rca.services.investigation import _reconcile_evidence, _writer_sections
from aiops_rca.tools.result import ToolExecutionResult

OUTPUT = {
    "sections": [
        {"id": "availability", "required": True},
        {"id": "capacity_trend", "required": True},
        {"id": "limitations", "required": True},
    ],
}


def _built_package():
    """Run the real builder and hand back what it produced."""

    scan = ToolExecutionResult(
        tool_call_id="call-1",
        tool_name="get_incident_events",
        source="zabbix",
        status="empty",
        request={"host_id": "11094"},
        response={"events": [], "result_count": 0},
        started_at=datetime(2026, 8, 19, tzinfo=UTC),
        finished_at=datetime(2026, 8, 19, tzinfo=UTC),
    )
    state = make_state(
        hosts=[ResolvedHost(host="vm-java-docker-2", host_id="11094")],
        hypotheses=[Hypothesis(id="h1", statement="용량 소진")],
        collection={
            "resolved_window": {
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-08-01T00:00:00Z",
            },
        },
        tool_results=[scan],
        tool_call_count=1,
        # An empty event query still produces evidence, which is what makes
        # incident_events covered: the window was searched.
        evidence=[
            Evidence.model_validate(
                {
                    "evidence_id": "zbx:event:none:abc123",
                    "evidence_type": "observation",
                    "source": "zabbix",
                    "summary": "No Zabbix problem event was returned.",
                    "observed_at": None,
                    "window": None,
                    "resource_ids": {
                        "host_id": "11094",
                        "event_id": None,
                        "trigger_id": None,
                        "item_id": None,
                    },
                    "metric": None,
                    "data_quality": None,
                    "tool_call_id": "call-1",
                    "search_query": None,
                },
            ),
        ],
        unknowns=[
            UnknownItem(
                code="coverage_collection_error",
                message="get_metric_summary failed",
            ),
        ],
    )
    import asyncio

    return asyncio.run(EvidencePackageBuilderNode()(state)), state


def test_the_builder_keeps_unknowns_as_plain_strings():
    update, _ = _built_package()
    package = update["evidence_package"]
    assert isinstance(package, EvidencePackage)
    assert package.unknowns
    assert all(isinstance(item, str) for item in package.unknowns)


def test_the_writer_path_runs_against_the_real_package():
    # The reason this file exists: _writer_sections read package.unknowns as
    # UnknownItem objects while the builder keeps only their messages, and every
    # test passed because the double held the richer type.
    update, _ = _built_package()
    sections = {
        item["id"]: item for item in _writer_sections(OUTPUT, update["evidence_package"])
    }
    assert set(sections) == {"availability", "capacity_trend", "limitations"}
    assert sections["capacity_trend"]["required"] is True


def test_reconciliation_runs_against_the_real_package():
    from aiops_rca.schemas.report import Report, ReportSection

    update, _ = _built_package()
    report = Report(
        title="2026년 7월 보고서",
        sections=[ReportSection(id="limitations", body="범위", items=[])],
    )
    # Must not raise, and must leave a package with no event evidence alone.
    assert _reconcile_evidence(report, OUTPUT, update["evidence_package"])
