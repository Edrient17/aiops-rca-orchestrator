"""Run the writer against stored evidence, and score what it writes.

The evidence package is frozen and the writer runs for real. That is the whole
design: re-running a full investigation to exercise the writer means three live
MCP servers and an answer that has moved since yesterday, while the defects the
writer actually produces -- a count off by one, a citation to nothing, a
truncation not passed on -- are all visible from a package that is already on
disk.

So an experiment here measures one thing and measures it repeatably: given
evidence this service really collected, does the writer still say something the
evidence supports.
"""

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiops_rca.evals.properties import CHECKS, Check
from aiops_rca.schemas.evidence_package import EvidencePackage
from aiops_rca.schemas.parsed_request import ParsedRequest
from aiops_rca.services.investigation import write_report

DATASET_NAME = "aiops-rca-writer"


@dataclass(frozen=True)
class Example:
    """One stored investigation, ready to be written up again."""

    request_id: str
    question: str
    parsed: ParsedRequest
    package: EvidencePackage
    template_output: dict[str, Any]
    #: What the writer said the first time. Kept for comparison, never as a
    #: target -- the report that shipped is not automatically the right answer,
    #: and several in this history are the defects that prompted the checks.
    previous_report: dict[str, Any]

    def as_inputs(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "question": self.question,
            "parsed": self.parsed.model_dump(mode="json"),
            "package": self.package.model_dump(mode="json", by_alias=True),
            "template_output": self.template_output,
        }


@dataclass(frozen=True)
class LoadReport:
    examples: list[Example]
    skipped: dict[str, int]
    #: One example of each skip reason, kept in full. A count alone says a row
    #: was dropped without saying what about it was unreadable, which turned a
    #: one-line fixture mistake into three rounds of guessing.
    reasons: dict[str, str]

    def __len__(self) -> int:
        return len(self.examples)


def load_examples(path: Path) -> LoadReport:
    """Read exported investigations, keeping the ones today's schemas accept.

    History spans schema versions and retired report kinds, and an example that
    cannot be loaded is not a failure worth reporting on every run -- it is a
    row from before the shape it is being read into existed. They are counted so
    the size of the set is never a surprise.
    """
    examples: list[Example] = []
    skipped: dict[str, int] = {}
    reasons: dict[str, str] = {}

    def skip(reason: str, detail: str = "") -> None:
        skipped[reason] = skipped.get(reason, 0) + 1
        reasons.setdefault(reason, detail)

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        template_output = row.get("template_output")
        if not isinstance(template_output, Mapping):
            skip("report kind no longer exists", str(row.get("request_id")))
            continue
        try:
            parsed = ParsedRequest.model_validate(row["parsed"])
            package = EvidencePackage.model_validate(row["package"])
        except Exception as error:
            skip("predates the current schema", f"{row.get('request_id')}: {error}")
            continue
        examples.append(
            Example(
                request_id=row["request_id"],
                question=row.get("question") or "",
                parsed=parsed,
                package=package,
                template_output=dict(template_output),
                previous_report=row.get("report") or {},
            ),
        )
    return LoadReport(examples=examples, skipped=skipped, reasons=reasons)


def writer_target(model: Any, model_name: str) -> Callable[[dict], Any]:
    """The thing under test: the writer, on evidence it does not have to collect."""

    async def target(inputs: dict) -> dict:
        parsed = ParsedRequest.model_validate(inputs["parsed"])
        package = EvidencePackage.model_validate(inputs["package"])
        report = await write_report(
            model,
            model_name,
            parsed=parsed,
            package=package,
            template_output=inputs["template_output"],
        )
        return {"report": report.model_dump(mode="json")}

    return target


def _as_evaluator(check: Check) -> Callable[..., dict]:
    """One property check in the shape LangSmith scores.

    A check that abstains scores 1 rather than being omitted. Omitting it would
    make an experiment's average rise whenever the evidence stopped being
    checkable, which is the opposite of what the number should do.
    """

    def evaluate(inputs: dict, outputs: dict) -> dict:
        findings = check(
            inputs.get("package") or {},
            (outputs or {}).get("report") or {},
            inputs.get("template_output") or {},
        )
        return {
            "key": check.__name__,
            "score": 0 if findings else 1,
            "comment": "; ".join(finding.detail for finding in findings)[:2000] or "ok",
        }

    evaluate.__name__ = check.__name__
    return evaluate


def evaluators(checks: Sequence[Check] = CHECKS) -> list[Callable[..., dict]]:
    return [_as_evaluator(check) for check in checks]


def baseline_findings(examples: Sequence[Example]) -> Iterator[tuple[str, list]]:
    """What the checks say about the reports that already shipped.

    The floor an experiment is measured against. Several of these reports are
    the defects the checks were written from, so a run that scores worse than
    this baseline has regressed against known-bad output.
    """
    from aiops_rca.evals.properties import check_report

    for example in examples:
        yield (
            example.request_id,
            check_report(
                example.package.model_dump(mode="json", by_alias=True),
                example.previous_report,
                example.template_output,
            ),
        )
