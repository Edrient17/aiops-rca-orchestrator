import json
from pathlib import Path

import pytest
from conftest import make_parsed_request
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from aiops_rca.schemas.evidence_package import EvidencePackage, EvidenceWindow
from aiops_rca.schemas.report import Report

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"


def assert_matches_source_schema(filename: str, payload: dict):
    schema = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


def test_parsed_request_preserves_existing_contract_and_rejects_extra_fields():
    parsed = make_parsed_request()
    assert parsed.schema_version == "0.1.0"
    assert parsed.anchor_time.utcoffset().total_seconds() == 0
    assert_matches_source_schema(
        "parsed-request.schema.json",
        parsed.model_dump(mode="json", by_alias=True),
    )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        type(parsed).model_validate(
            {**parsed.model_dump(), "guessed_root_cause": "OOM"}
        )


def test_report_accepts_omitted_optional_section_fields():
    report = Report.model_validate(
        {
            "schema_version": "0.1.0",
            "title": "장애 RCA",
            "sections": [{"id": "summary", "body": "조사 결과입니다."}],
        },
    )
    assert report.sections[0].items == []
    assert_matches_source_schema(
        "report.schema.json",
        report.model_dump(mode="json", by_alias=True),
    )


def test_evidence_package_keeps_existing_shape_and_validates_references():
    package = EvidencePackage.model_validate(
        {
            "schema_version": "0.1.0",
            "request": {
                "request_id": "req-1",
                "original_question": "왜 멈췄어?",
                "requested_by": "U123",
            },
            "query_context": {
                "hosts": [{"host": "vm-java-docker-2", "host_id": "11094"}],
                "timezone": "Asia/Seoul",
                "anchor_time": "2026-08-12T02:30:00Z",
            },
            "investigation": {
                "initial_window": {
                    "from": "2026-08-12T02:00:00Z",
                    "to": "2026-08-12T03:00:00Z",
                },
                "final_window": {
                    "from": "2026-08-12T02:00:00Z",
                    "to": "2026-08-12T03:00:00Z",
                },
                "iterations": 1,
                "tool_calls": [
                    {
                        "tool_call_id": "call-1",
                        "tool_name": "get_incident_events",
                        "purpose": "establish the event",
                        "status": "success",
                    }
                ],
                "expansion_reasons": [],
                "stop_reason": "evidence sufficient",
                "limit_reached": False,
            },
            "observed_failure_mode": "service problem event",
            "evidence": [
                {
                    "evidence_id": "zbx:event:44821",
                    "evidence_type": "event",
                    "source": "zabbix",
                    "summary": "service stopped",
                    "observed_at": "2026-08-12T02:22:40Z",
                    "window": {
                        "from": "2026-08-12T02:00:00Z",
                        "to": "2026-08-12T03:00:00Z",
                    },
                    "resource_ids": {
                        "host_id": "11094",
                        "event_id": "44821",
                        "trigger_id": "27714",
                        "item_id": None,
                    },
                    "metric": None,
                    "data_quality": None,
                    "tool_call_id": "call-1",
                }
            ],
            "confirmed_facts": [
                {"fact": "service stopped", "evidence_refs": ["zbx:event:44821"]}
            ],
            "hypotheses": [
                {
                    "description": "operator stop",
                    "supporting_evidence_refs": [],
                    "contradicting_evidence_refs": [],
                    "confidence": "low",
                }
            ],
            "unknowns": [],
        },
    )
    assert package.evidence[0].resource_ids.host_id == "11094"

    payload = package.model_dump(mode="json", by_alias=True)
    assert_matches_source_schema("evidence-package.schema.json", payload)
    payload["confirmed_facts"][0]["evidence_refs"] = ["zbx:event:missing"]
    with pytest.raises(ValidationError, match="evidence references do not exist"):
        EvidencePackage.model_validate(payload)


def test_window_serializes_existing_from_field_name():
    window = EvidenceWindow.model_validate(
        {"from": "2026-08-12T02:00:00Z", "to": "2026-08-12T03:00:00Z"},
    )
    assert (
        window.model_dump(mode="json", by_alias=True)["from"] == "2026-08-12T02:00:00Z"
    )
