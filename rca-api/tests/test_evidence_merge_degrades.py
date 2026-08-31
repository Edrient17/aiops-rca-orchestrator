"""Neither a repeated reading nor a full state may cost the investigation.

Both of these ended a run by raising, after every tool call had already been
paid for. The report was lost entirely -- not degraded, not partial -- over
bookkeeping that happens once all the real work is done.

An id is derived from the request, so asking the same question twice produces
the same id. If the answer moved in between, that was raised as a programming
error; it is not one. And the state's evidence ceiling was enforced by pydantic
after the fact, so the item that crossed it took the other two hundred with it.
"""

from datetime import UTC, datetime

import pytest

from aiops_rca.schemas.evidence_package import Evidence
from aiops_rca.tools.normalizer import EVIDENCE_CAPACITY, merge_evidence


def _evidence(evidence_id: str, summary: str = "observed") -> Evidence:
    return Evidence.model_validate(
        {
            "evidence_id": evidence_id,
            "evidence_type": "observation",
            "source": "zabbix",
            "summary": summary,
            "observed_at": datetime(2026, 8, 19, tzinfo=UTC),
            "window": None,
            "resource_ids": {
                "host_id": "11094",
                "event_id": None,
                "trigger_id": None,
                "item_id": None,
            },
            "metric": None,
            "data_quality": None,
            "tool_call_id": "call-1",
            "search_query": None,
        },
    )


class TestARepeatedReading:
    def test_the_later_one_is_kept(self):
        first = _evidence("zbx:object:a", "avg 10")
        second = _evidence("zbx:object:a", "avg 11")
        merged, _ = merge_evidence([first], [second])
        assert [item.summary for item in merged] == ["avg 11"]

    def test_the_disagreement_is_recorded(self):
        _, unknowns = merge_evidence(
            [_evidence("zbx:object:a", "avg 10")],
            [_evidence("zbx:object:a", "avg 11")],
        )
        assert [item.code for item in unknowns] == ["evidence_superseded"]
        assert "zbx:object:a" in unknowns[0].message

    def test_an_identical_reading_says_nothing(self):
        # Deduplication is ordinary and must not fill the report with notes.
        same = _evidence("zbx:object:a")
        merged, unknowns = merge_evidence([same], [same])
        assert len(merged) == 1
        assert unknowns == []


class TestTheCeiling:
    def test_collection_stops_rather_than_the_run(self):
        existing = [_evidence(f"zbx:object:{i}") for i in range(EVIDENCE_CAPACITY)]
        merged, unknowns = merge_evidence(existing, [_evidence("zbx:object:extra")])

        assert len(merged) == EVIDENCE_CAPACITY
        assert [item.code for item in unknowns] == ["evidence_capacity_reached"]

    def test_the_state_accepts_what_comes_back(self):
        # The point of the cap: whatever merge returns must validate, or the
        # crash simply moves one line later.
        from conftest import make_state

        existing = [_evidence(f"zbx:object:{i}") for i in range(EVIDENCE_CAPACITY)]
        merged, _ = merge_evidence(
            existing, [_evidence(f"zbx:object:over{i}") for i in range(20)]
        )
        state = make_state(evidence=merged)
        assert len(state.evidence) == EVIDENCE_CAPACITY

    def test_how_many_were_lost_is_stated(self):
        existing = [_evidence(f"zbx:object:{i}") for i in range(EVIDENCE_CAPACITY)]
        _, unknowns = merge_evidence(
            existing, [_evidence(f"zbx:object:over{i}") for i in range(7)]
        )
        assert "7" in unknowns[0].message

    def test_an_update_to_something_already_held_still_lands(self):
        # At capacity, replacing is not growth. Refusing it would freeze the
        # evidence at whatever happened to arrive first.
        existing = [_evidence(f"zbx:object:{i}") for i in range(EVIDENCE_CAPACITY)]
        merged, unknowns = merge_evidence(
            existing, [_evidence("zbx:object:0", "a newer reading")]
        )
        by_id = {item.evidence_id: item for item in merged}
        assert by_id["zbx:object:0"].summary == "a newer reading"
        assert [item.code for item in unknowns] == ["evidence_superseded"]


@pytest.mark.parametrize("capacity", [1, 5])
def test_the_ceiling_is_configurable_for_callers_that_need_less(capacity):
    merged, unknowns = merge_evidence(
        [], [_evidence(f"zbx:object:{i}") for i in range(capacity + 3)],
        capacity=capacity,
    )
    assert len(merged) == capacity
    assert unknowns and unknowns[-1].code == "evidence_capacity_reached"
