"""The month a monthly report covers is arithmetic, not a model's guess.

This branch had no test. It could not have one on Windows, where ZoneInfo finds
no tz database, so the one path that decides what "last month" means was the
one path never exercised. A month off by a day is a month of wrong data,
reported confidently.
"""

import pytest
from helpers import parsed_with_timezone

from aiops_rca.schemas.investigation import RequestEnvelope
from aiops_rca.services.templates import resolve_window


def _request(received_at: str) -> RequestEnvelope:
    return RequestEnvelope(
        request_id="req-1",
        source="slack",
        received_at=received_at,
        timezone="Asia/Seoul",
        question="지난달 어땠어",
        metadata={},
    )


def _month(received_at: str, timezone: str = "Asia/Seoul") -> dict[str, str]:
    return resolve_window(
        {"window": {"range": "last_calendar_month"}},
        parsed_with_timezone(timezone),
        _request(received_at),
    )


def test_a_request_in_august_covers_the_whole_of_july_in_local_time():
    # KST is UTC+9, so a local month starts at 15:00Z the previous day.
    assert _month("2026-08-18T01:00:00Z") == {
        "from": "2026-06-30T15:00:00Z",
        "to": "2026-07-31T15:00:00Z",
    }


def test_the_boundary_belongs_to_the_month_that_starts_there():
    # Asked one second into August local time, the answer is still July.
    assert _month("2026-07-31T15:00:01Z")["to"] == "2026-07-31T15:00:00Z"


def test_january_reaches_back_into_the_previous_year():
    assert _month("2026-01-15T00:00:00Z") == {
        "from": "2025-11-30T15:00:00Z",
        "to": "2025-12-31T15:00:00Z",
    }


def test_march_covers_february_and_its_real_length():
    window = _month("2026-03-10T00:00:00Z")
    assert window == {"from": "2026-01-31T15:00:00Z", "to": "2026-02-28T15:00:00Z"}


def test_a_leap_february_is_twenty_nine_days():
    from datetime import datetime

    window = _month("2028-03-10T00:00:00Z")
    span = datetime.fromisoformat(window["to"].replace("Z", "+00:00")) - datetime.fromisoformat(
        window["from"].replace("Z", "+00:00")
    )
    assert span.days == 29


@pytest.mark.parametrize(
    ("timezone", "expected_from"),
    [
        ("UTC", "2026-07-01T00:00:00Z"),
        ("Asia/Seoul", "2026-06-30T15:00:00Z"),
        ("America/New_York", "2026-07-01T04:00:00Z"),
    ],
)
def test_the_month_is_the_requester_s_month(timezone, expected_from):
    # Whose July it is depends on where the question came from, and a report
    # that used UTC for a KST operator would be nine hours out at both ends.
    assert _month("2026-08-18T01:00:00Z", timezone)["from"] == expected_from
