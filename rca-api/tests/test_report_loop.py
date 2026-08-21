"""Writing the report inside the graph, and recording what the checks found.

The writer used to run after the graph returned, so a miscount had nowhere to
go: written once, posted, and caught only by a person reading Slack. Inside the
graph the checks run against the finished report and what they find is kept.

They no longer send the draft back. Calibrated against a hundred and seventeen
reports from an older pipeline, every rejection inspected since has been the
check going stale rather than the report being wrong -- a count read as 261
because it was written 7,261, a number said to come from nowhere while it sat
in the evidence rows. A recorded finding is a line someone can dismiss; a fed
finding changes the report, and the way to satisfy a false one is to drop true
sentences.

What matters here is that the checks still run, that what they find is written
down, and that the run ends either way.
"""

import asyncio
from typing import Any

import pytest
from conftest import make_state

from aiops_rca.graph.report_nodes import (
    MAX_REPORT_ATTEMPTS,
    ReportEvalNode,
    ReportWriterNode,
)
from aiops_rca.graph.routing import route_after_report_eval
from aiops_rca.schemas.evidence_package import EvidencePackage
from aiops_rca.schemas.report import Report

PACKAGE = EvidencePackage.model_validate(
    {
        "schema_version": "0.1.0",
        "request": {
            "request_id": "REQ-1",
            "original_question": "트리거가 몇 개야",
            "requested_by": "U1",
        },
        "query_context": {
            "hosts": [{"host": "vm-java-docker-2", "host_id": "11094"}],
            "timezone": "Asia/Seoul",
            "anchor_time": "2026-08-20T00:00:00Z",
        },
        "investigation": {
            "initial_window": {
                "from": "2026-08-19T00:00:00Z",
                "to": "2026-08-20T00:00:00Z",
            },
            "final_window": {
                "from": "2026-08-19T00:00:00Z",
                "to": "2026-08-20T00:00:00Z",
            },
            "iterations": 1,
            "tool_calls": [],
            "expansion_reasons": [],
            "stop_reason": "done",
            "limit_reached": False,
        },
        "observed_failure_mode": "확인 요청",
        "confirmed_facts": [],
        "hypotheses": [],
        "evidence": [
            {
                "evidence_id": "zbx:object:11094:aaaa",
                "evidence_type": "observation",
                "source": "zabbix",
                "summary": "1 rows",
                "observed_at": "2026-08-20T00:00:00Z",
                "window": None,
                "resource_ids": {
                    "host_id": "11094",
                    "event_id": None,
                    "trigger_id": None,
                    "item_id": None,
                },
                "metric": None,
                "observed": {
                    "kind": "rows",
                    "omitted": 0,
                    "items": [{"triggers": [{"triggerid": str(i)} for i in range(26)]}],
                },
                "data_quality": None,
                "tool_call_id": "call-1",
                "search_query": None,
            },
        ],
        "unknowns": [],
    },
)

TEMPLATE_OUTPUT = {"sections": [{"id": "answer", "heading": "확인 결과", "required": True}]}


def _report(text: str) -> Report:
    return Report.model_validate(
        {
            "title": "확인",
            "sections": [
                {
                    "id": "answer",
                    "body": None,
                    "items": [
                        {
                            "text": text,
                            "label": None,
                            "evidence_refs": ["zbx:object:11094:aaaa"],
                            "counter_evidence_refs": [],
                        },
                    ],
                },
            ],
        },
    )


GOOD = _report("정의된 트리거: 26개")
BAD = _report("정의된 트리거: 25개")


class RecordingWriter:
    """Stands in for the model, and remembers what it was told."""

    def __init__(self, *drafts: Report) -> None:
        self.drafts = list(drafts)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Report:
        self.calls.append(kwargs)
        return self.drafts[min(len(self.calls) - 1, len(self.drafts) - 1)]


def _state(**updates: Any):
    return make_state(evidence_package=PACKAGE, template_output=TEMPLATE_OUTPUT, **updates)


def _write(writer: RecordingWriter, **updates: Any) -> dict[str, Any]:
    return dict(asyncio.run(ReportWriterNode(writer, "stub")(_state(**updates))))


def _check(report: Report | None, **updates: Any) -> dict[str, Any]:
    node = ReportEvalNode()
    return dict(asyncio.run(node(_state(report=report, **updates))))


class TestWriting:
    def test_the_report_is_written_from_state(self):
        writer = RecordingWriter(GOOD)
        update = _write(writer)
        assert update["report"] == GOOD
        assert update["report_attempts"] == 1
        assert writer.calls[0]["package"] is PACKAGE
        assert writer.calls[0]["template_output"] == TEMPLATE_OUTPUT

    def test_nothing_is_written_without_a_package(self):
        # A question that resolved no host reaches this node too. Reaching for a
        # model there would spend a call on nothing to write about.
        writer = RecordingWriter(GOOD)
        update = dict(
            asyncio.run(
                ReportWriterNode(writer, "stub")(make_state(template_output=TEMPLATE_OUTPUT)),
            ),
        )
        assert writer.calls == []
        assert "report" not in update

    def test_the_writer_is_told_what_was_rejected(self):
        # Without this it writes the same thing again, and the second draft
        # costs a model call to reproduce the first.
        writer = RecordingWriter(GOOD)
        _write(writer, report_findings=["counts_are_grounded: claims 25개"])
        assert writer.calls[0]["findings"] == ["counts_are_grounded: claims 25개"]

    def test_findings_do_not_outlive_the_draft_they_describe(self):
        writer = RecordingWriter(GOOD)
        update = _write(writer, report_findings=["stale"])
        assert update["report_findings"] == []

    def test_time_is_summed_across_drafts(self):
        # A report the checks sent back twice cost what all the passes cost.
        writer = RecordingWriter(GOOD)
        update = _write(writer, report_duration_ms=500)
        assert update["report_duration_ms"] >= 500


class TestChecking:
    def test_a_clean_draft_finds_nothing(self):
        assert _check(GOOD)["report_findings"] == []

    def test_a_miscount_is_reported_in_the_checker_s_words(self):
        findings = _check(BAD, report_attempts=1)["report_findings"]
        assert len(findings) == 1
        assert "counts_are_grounded" in findings[0]
        assert "25" in findings[0]

    def test_nothing_is_checked_without_a_report(self):
        # Claiming a clean result for a report that was never written would send
        # the run to __end__ looking like it had passed.
        update = _check(None)
        assert "report_findings" not in update
        assert "unknowns" not in update

    def test_the_last_draft_records_what_it_could_not_satisfy(self):
        # Out of drafts, the report goes out as it stands. A report that failed
        # its own checks and says so nowhere is what these checks exist to stop.
        update = _check(BAD, report_attempts=MAX_REPORT_ATTEMPTS)
        codes = [item.code for item in update["unknowns"]]
        assert codes == ["report_check_failed"]

    def test_what_sent_a_draft_back_is_kept_across_drafts(self):
        # report_findings describes only the draft in hand and the writer clears
        # it. A rewrite that succeeded used to leave no record of what was
        # wrong, so a check too eager to pass would cost a model call on every
        # report with nothing naming it.
        update = _check(BAD, report_attempts=1, report_rejections=["earlier"])
        assert update["report_rejections"][0] == "earlier"
        assert "counts_are_grounded" in update["report_rejections"][-1]

    def test_a_finding_is_recorded_on_the_draft_that_goes_out(self):
        # It used to be withheld until the last attempt, because an earlier
        # draft was about to be rewritten. Nothing is rewritten now, so the
        # first draft is the published one and its findings are the ones a
        # reader needs.
        update = _check(BAD, report_attempts=1)
        assert [item.code for item in update["unknowns"]] == ["report_check_failed"]
        assert update["report_rejections"]


class TestTheLoop:
    def test_a_clean_report_ends_the_run(self):
        assert route_after_report_eval(_state(report=GOOD)) == "__end__"

    def test_a_failing_report_also_ends_the_run(self):
        # The finding is recorded rather than acted on. Acting on it is what
        # cost a second writer pass on every investigation while the check was
        # rejecting numbers the evidence carried.
        state = _state(
            report=BAD, report_findings=["counts_are_grounded"], report_attempts=1
        )
        assert route_after_report_eval(state) == "__end__"

    @pytest.mark.parametrize("attempts", range(0, MAX_REPORT_ATTEMPTS + 3))
    def test_no_number_of_attempts_reopens_it(self, attempts):
        state = _state(report=BAD, report_findings=["x"], report_attempts=attempts)
        assert route_after_report_eval(state) == "__end__"
