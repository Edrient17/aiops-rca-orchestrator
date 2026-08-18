"""What a report section needs, in a form the machine can check.

A template used to be two free-form JSON blobs: `collection` said what to
gather, `output` said what to write, and nothing connected them. A section
could ask for month-over-month disk usage while the investigation never ran a
metric query, and the only way to find out was to read the finished report.

Sections therefore declare the effects they are built from, using the tool
registry's own vocabulary rather than a second one invented here. That single
declaration is what the coverage sweep collects against, what the stop guard
refuses to finish without, and what template validation checks at load time.
"""

from collections.abc import Iterable, Mapping
from typing import Annotated, Any

from pydantic import Field

from aiops_rca.schemas.base import StrictModel, TemplateId
from aiops_rca.tools.registry import ToolRegistry


class SectionContract(StrictModel):
    id: TemplateId
    heading: Annotated[str, Field(max_length=200)] | None = None
    instruction: Annotated[str, Field(max_length=4000)] | None = None
    required: bool = False
    # Empty means the section is written from whatever the investigation found,
    # which is the right answer for a narrative summary and the wrong one for a
    # section that reports specific measurements.
    requires_effects: tuple[str, ...] = ()
    requires_problem_event: bool = False


def parse_sections(output: Mapping[str, Any]) -> list[SectionContract]:
    """Read the section contracts out of a template's output block.

    Tolerant by design: templates are rows an operator edits, and a section
    that predates `requires_effects` still has to render. Absent means "no
    declared evidence", not "invalid".
    """
    sections = output.get("sections")
    if not isinstance(sections, list):
        return []
    parsed: list[SectionContract] = []
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        effects = section.get("requires_effects")
        parsed.append(
            SectionContract(
                id=str(section.get("id") or ""),
                heading=_text(section.get("heading")),
                instruction=_text(section.get("instruction")),
                required=bool(section.get("required")),
                requires_effects=tuple(
                    str(item) for item in effects if isinstance(item, str)
                )
                if isinstance(effects, list)
                else (),
                requires_problem_event=bool(section.get("requires_problem_event")),
            ),
        )
    return parsed


def declared_effects(sections: Iterable[SectionContract]) -> tuple[str, ...]:
    """Every effect the report will be written from, in a stable order."""

    return tuple(sorted({effect for item in sections for effect in item.requires_effects}))


def validate_template(
    output: Mapping[str, Any],
    registry: ToolRegistry,
    *,
    obtainable_effects: Iterable[str] = (),
) -> list[str]:
    """Reasons this template cannot produce the report it promises.

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

    known = set(registry.effects())
    obtainable = set(obtainable_effects)
    for section in sections:
        for effect in section.requires_effects:
            if effect not in known:
                problems.append(
                    f"section {section.id} requires effect {effect!r}, which no"
                    " allowlisted tool produces",
                )
            elif obtainable and effect not in obtainable:
                # Producible on request but not reachable on its own: a section
                # built from it would depend on the planner happening to ask.
                problems.append(
                    f"section {section.id} requires effect {effect!r}, which no"
                    " coverage recipe can collect deterministically",
                )
    return problems


def _text(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value.strip() else None
