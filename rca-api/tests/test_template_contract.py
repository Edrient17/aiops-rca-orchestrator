"""Every shipped template must be able to produce the report it promises.

The failure this prevents is quiet: a section asks for an observation nothing
in the system can make, the investigation runs, and the section comes out empty
with no reason given. That is only visible by reading the finished report, and
by then it has already been sent.
"""

import json
from pathlib import Path

import pytest

from aiops_rca.services.template_contract import (
    declared_effects,
    parse_sections,
    validate_template,
)
from aiops_rca.tools.coverage import obtainable_effects
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"
TEMPLATES = sorted(TEMPLATE_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_template_directory_is_where_this_test_thinks_it_is():
    # A moved directory would silently turn every check below into a no-op.
    assert TEMPLATES, f"no templates found under {TEMPLATE_DIR}"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_a_shipped_template_can_produce_its_own_report(path: Path):
    problems = validate_template(
        _load(path)["output"],
        DEFAULT_TOOL_REGISTRY,
        obtainable_effects=obtainable_effects(),
    )
    assert problems == [], f"{path.name}: " + "; ".join(problems)


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_a_shipped_template_declares_at_least_one_section(path: Path):
    assert parse_sections(_load(path)["output"])


def test_the_monthly_report_declares_the_measurements_it_is_made_of():
    output = _load(TEMPLATE_DIR / "monthly-capacity-report.json")["output"]
    by_id = {item.id: item for item in parse_sections(output)}
    # These are the two sections that came out empty in production: the report
    # is a month of measurements, and nothing had said so.
    assert by_id["capacity_trend"].requires_effects == ("metric_change",)
    assert by_id["resource_pressure"].requires_effects == (
        "metric_level",
        "metric_trend",
    )
    assert by_id["availability"].requires_effects == ("incident_events",)


def test_a_narrative_section_declares_nothing():
    output = _load(TEMPLATE_DIR / "monthly-capacity-report.json")["output"]
    by_id = {item.id: item for item in parse_sections(output)}
    # Declaring an effect here would make a summary that reads the whole
    # investigation depend on one particular observation existing.
    assert by_id["summary"].requires_effects == ()
    assert by_id["limitations"].requires_effects == ()


def test_an_incident_template_does_not_force_a_capacity_sweep():
    # Incident sections are conclusions, not measurements. A declaration here
    # would buy a metric sweep on every incident whether or not it helps.
    output = _load(TEMPLATE_DIR / "incident-rca.json")["output"]
    assert declared_effects(parse_sections(output)) == ()


class TestWhatValidationCatches:
    def test_an_effect_no_tool_produces(self):
        output = {
            "sections": [
                {"id": "topology", "requires_effects": ["network_topology"]},
            ],
        }
        problems = validate_template(output, DEFAULT_TOOL_REGISTRY)
        assert any("no allowlisted tool produces" in item for item in problems)

    def test_an_effect_no_recipe_can_collect(self):
        # `raw_log_evidence` is real and reachable when a planner asks for it,
        # but a section built on it would depend on that happening.
        output = {
            "sections": [{"id": "logs", "requires_effects": ["raw_log_evidence"]}],
        }
        problems = validate_template(
            output,
            DEFAULT_TOOL_REGISTRY,
            obtainable_effects=obtainable_effects(),
        )
        assert any("no coverage recipe" in item for item in problems)

    def test_a_duplicate_section_id(self):
        output = {"sections": [{"id": "summary"}, {"id": "summary"}]}
        assert "duplicate section id: summary" in validate_template(
            output, DEFAULT_TOOL_REGISTRY
        )

    def test_a_template_with_no_sections(self):
        assert validate_template({}, DEFAULT_TOOL_REGISTRY) == [
            "template declares no sections",
        ]

    def test_a_section_predating_the_contract_still_loads(self):
        # Templates are rows an operator edits. Absent means "no declared
        # evidence", not "invalid", or an upgrade would break every stored row.
        output = {"sections": [{"id": "summary", "instruction": "요약을 쓴다"}]}
        assert validate_template(output, DEFAULT_TOOL_REGISTRY) == []
