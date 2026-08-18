"""Shared builders for tests that need a parsed request in a given timezone."""

from aiops_rca.schemas.parsed_request import ParsedRequest


def parsed_with_timezone(timezone: str) -> ParsedRequest:
    return ParsedRequest(
        request_id="req-1",
        parse_status="ready",
        request_type="monthly_capacity_report",
        host_queries=["vm-java-docker-2"],
        anchor_time=None,
        timezone=timezone,
        incident_description="지난달 전반 상태",
        incident_type_hint=None,
        user_intent="지난달 상태 확인",
        initial_window_hint=None,
        allow_dynamic_expansion=True,
        ambiguities=[],
        original_question="지난달 어땠어",
    )
