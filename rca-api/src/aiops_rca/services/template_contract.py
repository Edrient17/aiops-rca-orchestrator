"""What a report section is, in a form the machine can read.

A template is two JSON blobs: `collection` says what to gather, `output` says
what to write. Sections used to also declare the observations they were written
from, in the tool registry's own vocabulary, and a sweep collected against that
declaration before the report was written.

That guarantee was real and expensive. The declaration drove collection whether
the question needed it or not, and adding a report kind meant touching the
registry and the collection recipes as well as writing the template. Whether a
section could be filled is judged after the report is written now: a required
one left empty with nothing said about why sends the draft back to the writer.

What is left here describes the document -- ids, headings, which sections are
required, which need a real problem event behind them.
"""

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field

from aiops_rca.schemas.base import StrictModel, TemplateId


class SectionContract(StrictModel):
    id: TemplateId
    heading: Annotated[str, Field(max_length=200)] | None = None
    instruction: Annotated[str, Field(max_length=4000)] | None = None
    required: bool = False
    requires_problem_event: bool = False


def parse_sections(output: Mapping[str, Any]) -> list[SectionContract]:
    """Read the section contracts out of a template's output block.

    Tolerant by design: templates are rows an operator edits, and a section
    that omits everything optional still has to render.
    """
    sections = output.get("sections")
    if not isinstance(sections, list):
        return []
    parsed: list[SectionContract] = []
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        parsed.append(
            SectionContract(
                id=str(section.get("id") or ""),
                heading=_text(section.get("heading")),
                instruction=_text(section.get("instruction")),
                required=bool(section.get("required")),
                requires_problem_event=bool(section.get("requires_problem_event")),
            ),
        )
    return parsed


def validate_template(output: Mapping[str, Any]) -> list[str]:
    """Reasons this template cannot produce the report it promises.

    Structural only now. It used to also check that every effect a section
    declared was one some allowlisted tool produced and some recipe could
    collect -- a real guarantee, and the reason adding a report kind meant
    touching the registry and the recipes as well as writing the template.

    What a section needs is decided after the report is written: leave a
    required one empty and say nothing about why, and the draft comes back.

    Returning a list rather than raising lets a caller report every problem in
    one pass, which is what an operator adding a template wants to see.
    """
    problems: list[str] = []
    sections = parse_sections(output)
    if not sections:
        return ["template declares no sections"]

    seen: set[str] = set()
    for section in sections:
        if section.id in seen:
            problems.append(f"duplicate section id: {section.id}")
        seen.add(section.id)
    return problems


def _text(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value.strip() else None
