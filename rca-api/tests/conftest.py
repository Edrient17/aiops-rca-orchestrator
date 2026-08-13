import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aiops_rca.graph.state import InvestigationState
from aiops_rca.schemas.investigation import RequestEnvelope
from aiops_rca.schemas.parsed_request import ParsedRequest

FIXTURES = Path(__file__).parent / "fixtures" / "mcp"


@pytest.fixture
def fixture_json():
    def load(relative: str) -> Any:
        return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))

    return load


def make_parsed_request(*, host_queries: list[str] | None = None) -> ParsedRequest:
    return ParsedRequest(
        request_id="req-1",
        parse_status="ready",
        request_type="incident_rca",
        host_queries=host_queries or ["vm-java-docker-2"],
        anchor_time="2026-08-12T02:30:00Z",
        timezone="Asia/Seoul",
        incident_description="payment-service stopped",
        incident_type_hint="service_stop",
        user_intent="identify the cause",
        initial_window_hint={"before_minutes": 30, "after_minutes": 30},
        allow_dynamic_expansion=True,
        ambiguities=[],
        original_question="payment-service가 왜 멈췄어?",
    )


def make_state(
    *, host_queries: list[str] | None = None, **updates: Any
) -> InvestigationState:
    data: dict[str, Any] = {
        "investigation_id": "inv-1",
        "request": RequestEnvelope(
            request_id="req-1",
            source="slack",
            received_at="2026-08-12T02:31:00Z",
            timezone="Asia/Seoul",
            question="payment-service가 왜 멈췄어?",
            metadata={"user_id": "U123"},
        ),
        "parsed_request": make_parsed_request(host_queries=host_queries),
        "started_at": datetime(2026, 8, 12, 2, 31, tzinfo=UTC),
    }
    data.update(updates)
    return InvestigationState.model_validate(data)
