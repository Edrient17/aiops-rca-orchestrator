"""Two places a value from outside was carried further than it could go.

An evidence id a server supplied was written straight into a model that
accepts only the prefixes this service declares. An unknown recorded for an
operator was written straight into a field the shared contract bounds at
twenty entries of five hundred characters. Neither had anything between the
value and the place it ended up.
"""

from datetime import UTC, datetime

import pytest

from aiops_rca.schemas.investigation import PlannedToolCall, UnknownItem
from aiops_rca.services.investigation import (
    MAX_AMBIGUITIES,
    MAX_AMBIGUITY_CHARS,
    _clarification_lines,
)
from aiops_rca.tools.normalizer import normalize_observation
from aiops_rca.tools.result import ToolExecutionResult

NOW = datetime(2026, 8, 21, tzinfo=UTC)
PLANNED = PlannedToolCall(
    tool_name="get_incident_events",
    arguments={"time_from": "2026-08-20T00:00:00Z", "time_to": "2026-08-21T00:00:00Z"},
    purpose="이벤트가 있었는가",
    target_hypothesis_ids=[],
    host="vm-known",
)


def events(*items) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_call_id="call-1",
        tool_name="get_incident_events",
        source="zabbix",
        status="ok",
        request=dict(PLANNED.arguments),
        response={"events": list(items)},
        started_at=NOW,
        finished_at=NOW,
    )


def normalize(result: ToolExecutionResult):
    return normalize_observation(result, PLANNED, host_id="11094", host="vm-known")


class TestAnIdTheServerSupplied:
    """`evidence_id` was read off the response and used as it stood.

    The schema accepts only the prefixes `sources.py` declares, so a server
    sending anything else raised a ValidationError out of the normalizer and
    out of the graph -- ending an investigation that had already paid for
    every one of its tool calls.
    """

    def test_an_id_this_service_cannot_carry_does_not_end_the_run(self):
        produced = normalize(events({"evidence_id": "zabbix-event-9", "event_id": "9"}))
        assert [item.evidence_id for item in produced] == ["zbx:event:9"]

    def test_an_id_it_can_carry_is_kept(self):
        produced = normalize(
            events({"evidence_id": "zbx:event:custom-9", "event_id": "9"})
        )
        assert [item.evidence_id for item in produced] == ["zbx:event:custom-9"]

    @pytest.mark.parametrize("offered", [None, "", 9, {"id": 9}, "log:", "nonsense"])
    def test_anything_unusable_falls_back_rather_than_raising(self, offered):
        produced = normalize(events({"evidence_id": offered, "event_id": "9"}))
        assert [item.evidence_id for item in produced] == ["zbx:event:9"]


class TestAnEventWithNoNumericId:
    """They all became the literal string "zbx:event:None".

    merge_evidence keys on the id, so a window of distinct events collapsed to
    one row plus a line saying the later reading was kept.
    """

    def test_two_such_events_stay_two(self):
        produced = normalize(
            events(
                {"name": "disk filling", "event_id": None},
                {"name": "service restarted", "event_id": "n/a"},
            )
        )
        assert len({item.evidence_id for item in produced}) == 2
        assert all("None" not in item.evidence_id for item in produced)

    def test_the_same_event_read_twice_keeps_one_id(self):
        # The id is derived from the event, so it is stable -- which is what
        # lets merge_evidence recognise a re-read rather than double-count it.
        first = normalize(events({"name": "disk filling", "event_id": None}))
        second = normalize(events({"name": "disk filling", "event_id": None}))
        assert first[0].evidence_id == second[0].evidence_id

    def test_an_event_that_has_an_id_still_uses_it(self):
        produced = normalize(events({"name": "disk filling", "event_id": "77"}))
        assert [item.evidence_id for item in produced] == ["zbx:event:77"]


class TestWhatTheAskerIsSentBack:
    """Every unknown went into `ambiguities`, whole.

    The state holds a hundred at two thousand characters each; the contract
    published in `schemas/parsed-request.schema.json` is twenty at five
    hundred. Nothing enforced it -- model_copy does not validate, and a model
    instance is not re-validated into the response -- so a run that resolved no
    host could answer with a wall of internal diagnostics, or overrun Slack's
    limit and fail the post outright.
    """

    def _unknowns(self, count: int, code: str = "tool_error"):
        return [
            UnknownItem(code=code, message=f"{code} {index}: " + "x" * 900)
            for index in range(count)
        ]

    def test_the_contract_s_item_limit_is_respected(self):
        lines = _clarification_lines(self._unknowns(60))
        assert len(lines) == MAX_AMBIGUITIES

    def test_the_contract_s_length_limit_is_respected(self):
        lines = _clarification_lines(self._unknowns(3))
        assert all(len(line) <= MAX_AMBIGUITY_CHARS for line in lines)

    def test_what_the_asker_can_act_on_survives_the_cut(self):
        # A host that could not be resolved is the one thing a reply can fix.
        # Recorded last and cut first, it was the entry that never arrived.
        noise = self._unknowns(40)
        actionable = UnknownItem(
            code="host_not_found",
            message="No host matched 'vm-typo' in any source searched.",
            host_query="vm-typo",
        )
        lines = _clarification_lines([*noise, actionable])
        assert lines[0].startswith("No host matched 'vm-typo'")

    def test_an_ambiguous_host_leads_as_well(self):
        lines = _clarification_lines(
            [
                *self._unknowns(30),
                UnknownItem(code="host_ambiguous", message="Several hosts matched."),
            ]
        )
        assert lines[0] == "Several hosts matched."

    def test_a_run_that_recorded_nothing_still_asks_something(self):
        assert _clarification_lines([]) == [
            "조사할 호스트를 하나 이상 확인할 수 없습니다."
        ]

    def test_the_result_satisfies_the_field_it_is_written_into(self):
        from aiops_rca.schemas.parsed_request import ParsedRequest

        field = ParsedRequest.model_fields["ambiguities"]
        lines = _clarification_lines(self._unknowns(60))
        # What the model would reject, rather than a number repeated here.
        assert len(lines) <= next(
            item.max_length for item in field.metadata if hasattr(item, "max_length")
        )
