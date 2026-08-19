"""Window boundaries must be a shape the MCPs accept.

A question about the present names no time, so the anchor falls back to the
request's arrival -- and that comes off the ingress clock with microseconds.
`isoformat()` printed six fractional digits, the Zabbix MCP accepts three, and
the investigation failed at its first event scan with

    time_from must be an ISO 8601 timestamp with a timezone

which reads like a malformed request rather than what it was: a boundary too
precise for the thing being asked. Zabbix stores event times as epoch seconds,
so the extra digits could not have selected anything anyway.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest
from helpers import parsed_with_timezone

from aiops_rca.schemas.investigation import RequestEnvelope
from aiops_rca.services.templates import resolve_window

# The Zabbix MCP's own pattern, copied so a change there fails here too.
ISO_WITH_TIMEZONE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?(?:Z|[+-]\d{2}:\d{2})$",
)


def _request(received_at: datetime) -> RequestEnvelope:
    return RequestEnvelope(
        request_id="req-1",
        source="slack",
        received_at=received_at,
        timezone="Asia/Seoul",
        question="지금 뭐가 돌고 있어",
        metadata={},
    )


ARRIVED_WITH_MICROSECONDS = datetime(2026, 8, 19, 2, 42, 14, 423000, tzinfo=UTC)


@pytest.mark.parametrize(
    "collection",
    [
        {"window": {"range": "anchor_relative"}},
        {"window": {"range": "last_7_days"}},
        {"window": {"range": "last_30_days"}},
        {"window": {"range": "last_calendar_month"}},
    ],
    ids=lambda c: c["window"]["range"],
)
def test_every_range_produces_a_boundary_the_mcp_accepts(collection):
    window = resolve_window(
        collection,
        parsed_with_timezone("Asia/Seoul"),
        _request(ARRIVED_WITH_MICROSECONDS),
    )
    for edge in ("from", "to"):
        assert ISO_WITH_TIMEZONE.match(window[edge]), (edge, window[edge])


def test_the_arrival_clock_does_not_leak_microseconds():
    # This is the case that failed: no anchor in the question, so the window is
    # built from received_at, microseconds and all.
    window = resolve_window(
        {"window": {"range": "anchor_relative"}},
        parsed_with_timezone("Asia/Seoul"),
        _request(ARRIVED_WITH_MICROSECONDS),
    )
    assert ".423" not in window["from"]
    assert window["from"].endswith("Z")


def test_the_window_still_spans_what_it_should():
    # Truncation must not move a boundary by more than the second it drops.
    parsed = parsed_with_timezone("Asia/Seoul")
    window = resolve_window(
        {"window": {"range": "last_7_days"}}, parsed, _request(ARRIVED_WITH_MICROSECONDS)
    )
    start = datetime.fromisoformat(window["from"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(window["to"].replace("Z", "+00:00"))
    assert end - start == timedelta(days=7)


def test_an_anchor_from_the_question_is_unaffected():
    # An analyzer-supplied anchor is already whole seconds; nothing changes.
    window = resolve_window(
        {"window": {"range": "anchor_relative"}},
        parsed_with_timezone("Asia/Seoul"),
        _request(datetime(2026, 8, 19, 2, 42, 14, tzinfo=UTC)),
    )
    assert window["from"] == "2026-08-18T23:42:14Z"
