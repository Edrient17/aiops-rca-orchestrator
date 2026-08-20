"""Writing the report, and checking it against the evidence it cites.

The writer used to run after the graph returned, which put the last and most
error-prone step of an investigation outside everything the graph provides.
A miscount there was invisible to checkpointing, sat in the trace as a separate
thing from the run that produced its evidence, and -- worst -- had nowhere to be
sent back to. The report was written once and posted.

Inside, it can be written again. `report_eval` runs the same deterministic
checks the offline harness scores experiments with, and a draft that fails them
goes back to the writer with the findings attached.
"""

from collections.abc import Mapping
from time import perf_counter
from typing import Any

from aiops_rca.evals.properties import check_report
from aiops_rca.graph.state import InvestigationState
from aiops_rca.schemas.investigation import UnknownItem

#: Drafts, not retries: the first one counts. Two is enough for the failures
#: these checks find -- a count copied wrong, a citation to nothing -- and a
#: third costs a model call to relitigate something the writer has already been
#: told twice.
MAX_REPORT_ATTEMPTS = 2


class ReportWriterNode:
    """Turn the finished evidence package into the report.

    Takes the writing function rather than reaching for it, so a test can drive
    this node without a model and the service can keep owning how its client is
    built.
    """

    def __init__(self, write: Any, model_name: str) -> None:
        self.write = write
        self.model_name = model_name

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        package = state.evidence_package
        if package is None or state.template_output is None:
            # An investigation that resolved no host, or stopped before it had
            # anything to write about. There is no report to fail at, and
            # saying so here is what keeps the router simple.
            return {"visited_nodes": [*state.visited_nodes, "report_writer"]}

        started = perf_counter()
        report = await self.write(
            parsed=state.parsed_request,
            package=package,
            template_output=state.template_output,
            uncovered_effects=state.uncovered_effects,
            findings=state.report_findings,
        )
        elapsed = int((perf_counter() - started) * 1000)

        return {
            "report": report,
            "report_attempts": state.report_attempts + 1,
            "report_duration_ms": state.report_duration_ms + elapsed,
            # Cleared on the way out: they describe the draft that was just
            # replaced, and leaving them would have the next check reported
            # against a report they were never about.
            "report_findings": [],
            "visited_nodes": [*state.visited_nodes, "report_writer"],
        }


class ReportEvalNode:
    """Hold the report against the evidence it was written from.

    No model. Every check asks whether the report is consistent with its own
    evidence, which is answerable from the two documents and needs no opinion
    about what the right answer was.
    """

    def __init__(self, max_attempts: int = MAX_REPORT_ATTEMPTS) -> None:
        self.max_attempts = max_attempts

    async def __call__(self, state: InvestigationState) -> Mapping[str, Any]:
        visited = [*state.visited_nodes, "report_eval"]
        if state.report is None or state.evidence_package is None:
            return {"visited_nodes": visited}

        findings = check_report(
            state.evidence_package.model_dump(mode="json", by_alias=True),
            state.report.model_dump(mode="json"),
            state.template_output or {},
        )
        if not findings:
            return {"report_findings": [], "visited_nodes": visited}

        detail = [
            f"{item.check} ({item.section_id or 'report'}): {item.detail}"
            for item in findings
        ]
        if state.report_attempts < self.max_attempts:
            return {"report_findings": detail, "visited_nodes": visited}

        # Out of drafts. The report goes out as it stands -- the writer was told
        # about these on its last pass and had the chance to say so -- but the
        # findings are recorded, because a report that failed its own checks and
        # says nothing about it anywhere is the thing these checks exist to stop.
        return {
            "report_findings": detail,
            "unknowns": [
                *state.unknowns,
                *(
                    UnknownItem(code="report_check_failed", message=message)
                    for message in detail
                ),
            ],
            "visited_nodes": visited,
        }
