"""The window an investigation opens, from what the analyzer said about time.

Execution 139 asked about 어제 and looked at one hour around midnight. The
analyzer had done nothing wrong: initial_window_hint could only express minutes
either side of an anchor, so a question about a day had to be pressed into a
moment, and resolve_window then fell back to its narrowest default.
"""

from datetime import UTC, datetime

import pytest
from conftest import make_parsed_request
from pydantic import ValidationError

from aiops_rca.schemas.investigation import RequestEnvelope
from aiops_rca.services.templates import resolve_window

RECEIVED = datetime(2026, 8, 13, 0, 30, tzinfo=UTC)
ANCHOR_RELATIVE = {"window": {"range": "anchor_relative"}}


def envelope() -> RequestEnvelope:
    return RequestEnvelope(
        request_id="REQ-1",
        source="slack",
        received_at=RECEIVED,
        timezone="Asia/Seoul",
        question="어제 vm-java-docker-2에서 전체적으로 문제 있었는지 확인해줘",
    )


def resolve(hint, *, anchor="2026-08-12T00:00:00+09:00"):
    """Rebuild the parsed request so the hint goes through real validation."""
    fields = make_parsed_request().model_dump(mode="json", by_alias=True)
    fields["anchor_time"] = anchor
    fields["initial_window_hint"] = hint
    parsed = type(make_parsed_request()).model_validate(fields)
    return resolve_window(ANCHOR_RELATIVE, parsed, envelope())


def test_an_interval_question_opens_that_interval():
    # 어제, in Asia/Seoul, is 2026-08-11T00:00+09:00 .. 2026-08-12T00:00+09:00.
    window = resolve(
        {"from": "2026-08-11T00:00:00+09:00", "to": "2026-08-12T00:00:00+09:00"},
    )
    assert window == {"from": "2026-08-10T15:00:00Z", "to": "2026-08-11T15:00:00Z"}

    # The operator command that execution 139 never reached sits inside it.
    stop = datetime(2026, 8, 11, 2, 33, 34, tzinfo=UTC)
    assert datetime.fromisoformat(window["from"].replace("Z", "+00:00")) <= stop
    assert stop <= datetime.fromisoformat(window["to"].replace("Z", "+00:00"))


def test_a_moment_question_still_reads_as_minutes_either_side():
    window = resolve(
        {"before_minutes": 60, "after_minutes": 30},
        anchor="2026-08-12T11:00:00+09:00",
    )
    assert window == {"from": "2026-08-12T01:00:00Z", "to": "2026-08-12T02:30:00Z"}


def test_an_absent_hint_no_longer_collapses_to_half_an_hour():
    # The old default was thirty minutes either side -- the narrowest window
    # this system opens -- so a missing field silently blinded the collector.
    window = resolve(None, anchor="2026-08-12T11:00:00+09:00")
    assert window == {"from": "2026-08-11T23:00:00Z", "to": "2026-08-12T05:00:00Z"}


@pytest.mark.parametrize(
    "hint",
    [
        {"from": "2026-08-12T00:00:00+09:00", "to": "2026-08-11T00:00:00+09:00"},
        {"from": "2026-08-11T00:00:00+09:00"},
        {"before_minutes": 720, "after_minutes": 720,
         "from": "2026-08-11T00:00:00+09:00", "to": "2026-08-12T00:00:00+09:00"},
    ],
    ids=["end before start", "half an interval", "both shapes at once"],
)
def test_malformed_hints_are_refused(hint):
    # Named rather than blind: a hint that is refused for the wrong reason,
    # or by a typo in this test, would otherwise read as a pass.
    with pytest.raises(ValidationError):
        resolve(hint)
