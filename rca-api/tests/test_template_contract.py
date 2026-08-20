"""Every shipped template must describe a report that can be rendered.

This used to check more. A section declared the observations it was written
from, and the check proved that every one of them was something an allowlisted
tool produced and a recipe could collect -- so a template that promised a
section nothing could fill was refused before it shipped.

That guarantee moved. It cost a tool call on every run whether the question
needed one or not, and it meant adding a report kind touched the registry and
the recipes as well as the template file. Whether a section could be filled is
now decided after the report is written: a required one left empty with nothing
said about why sends the draft back to the writer.

What is left here is structural, and it is what a template file can get wrong on
its own.
"""

import json
from pathlib import Path

import pytest

from aiops_rca.services.template_contract import parse_sections, validate_template

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"
TEMPLATES = sorted(TEMPLATE_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_template_directory_is_where_this_test_thinks_it_is():
    # Every case below passes vacuously against an empty glob.
    assert TEMPLATES, f"no templates found under {TEMPLATE_DIR}"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_a_shipped_template_is_structurally_sound(path: Path):
    assert validate_template(_load(path)["output"]) == []


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_every_section_has_an_id_and_a_heading(path: Path):
    # The template owns the layout; a section with no heading renders as an
    # unlabelled block, and one with no id can never be filled.
    for section in parse_sections(_load(path)["output"]):
        assert section.id, f"{path.stem} has a section with no id"
        assert section.heading, f"{path.stem}:{section.id} has no heading"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_every_template_declares_a_limitations_section(path: Path):
    # Three of the six report checks decide whether a report admitted what it
    # could not do, and they read this section. A template without one gives
    # them nowhere to look and the answer to "what did this miss" is silence.
    ids = {section.id for section in parse_sections(_load(path)["output"])}
    assert "limitations" in ids


def test_a_template_with_no_sections_is_refused():
    assert validate_template({"sections": []}) == ["template declares no sections"]


def test_duplicate_section_ids_are_refused():
    # The writer answers by section id, so two sections sharing one means the
    # second silently replaces the first.
    problems = validate_template(
        {
            "sections": [
                {"id": "summary", "heading": "요약"},
                {"id": "summary", "heading": "다시 요약"},
            ],
        },
    )
    assert problems == ["duplicate section id: summary"]


def test_a_section_missing_everything_optional_still_parses():
    # Templates are rows an operator edits. A missing optional field means the
    # default, not a broken template.
    sections = parse_sections({"sections": [{"id": "answer"}]})
    assert len(sections) == 1
    assert sections[0].required is False
    assert sections[0].requires_problem_event is False
