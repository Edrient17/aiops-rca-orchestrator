"""A bad citation must not cost the investigation that produced it.

Execution 149 -- the first month-long report to run on the graph -- died at
hypothesis_updater with "hypothesis update references unknown evidence". The
whole call raised, so nothing was persisted: not the evidence already
collected, not the question analysis that had already succeeded. The audit
tables show that request with zero agent runs.
"""

import pytest
from pydantic import ValidationError

from aiops_rca.services.model_contracts import (
    HypothesisUpdateDecision,
    hypothesis_update_decision_for,
)

EVIDENCE = ("zbx:event:11094:down", "wazuh:alerts:vm-1:stop")
HYPOTHESES = ("H1", "H2")


def update(**overrides):
    base = {
        "hypothesis_id": "H1",
        "status": "supported",
        "supporting_evidence_ids": ["wazuh:alerts:vm-1:stop"],
        "counter_evidence_ids": [],
        "rationale": "명령 기록이 중단 시각과 일치함",
    }
    return {**base, **overrides}


def decision(**overrides):
    base = {
        "updates": [update()],
        "new_hypotheses": [],
        "new_facts": [],
        "stop_reason": None,
    }
    return {**base, **overrides}


def test_real_ids_are_accepted():
    bound = hypothesis_update_decision_for(EVIDENCE, HYPOTHESES)
    assert bound.model_validate(decision())


def test_an_evidence_id_that_does_not_exist_cannot_be_produced():
    bound = hypothesis_update_decision_for(EVIDENCE, HYPOTHESES)
    with pytest.raises(ValidationError):
        bound.model_validate(
            decision(updates=[update(supporting_evidence_ids=["zbx:event:11094:typo"])]),
        )


def test_a_hypothesis_id_that_does_not_exist_cannot_be_produced():
    bound = hypothesis_update_decision_for(EVIDENCE, HYPOTHESES)
    with pytest.raises(ValidationError):
        bound.model_validate(decision(updates=[update(hypothesis_id="H9")]))


def test_citing_nothing_stays_legal():
    # An observation that moves no hypothesis is a real outcome, and narrowing
    # the field must not turn it into a validation error.
    bound = hypothesis_update_decision_for(EVIDENCE, HYPOTHESES)
    assert bound.model_validate(
        decision(updates=[update(status="active", supporting_evidence_ids=[])]),
    )


@pytest.mark.parametrize(
    ("evidence", "hypotheses"),
    [((), HYPOTHESES), (EVIDENCE, ()), ((), ())],
    ids=["no evidence yet", "no hypotheses yet", "neither"],
)
def test_the_contract_stays_open_before_there_is_anything_to_bind_to(
    evidence, hypotheses
):
    assert (
        hypothesis_update_decision_for(evidence, hypotheses)
        is HypothesisUpdateDecision
    )


class _Model:
    """Answers with whatever decision the test wants, ignoring the bound type."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def complete(self, *, output_type, **_kwargs):
        # Deliberately bypasses the bound type: this exercises what the node
        # does when a citation slips through anyway, which is the path that
        # used to discard the run.
        return HypothesisUpdateDecision.model_validate(self.payload)


@pytest.mark.asyncio
async def test_an_unresolvable_citation_is_dropped_rather_than_fatal():
    import asyncio  # noqa: F401  (marker requires an event loop)
    from datetime import UTC, datetime

    from conftest import make_state

    from aiops_rca.graph.live_nodes import HypothesisUpdaterNode
    from aiops_rca.schemas.investigation import Hypothesis, ResolvedHost
    from aiops_rca.tools.result import ToolExecutionResult

    now = datetime(2026, 8, 13, 7, tzinfo=UTC)
    observation = ToolExecutionResult(
        tool_call_id="call-1",
        tool_name="get_wazuh_alert_summary",
        source="wazuh",
        status="ok",
        request={},
        response={"alerts": []},
        started_at=now,
        finished_at=now,
    )
    state = make_state(
        hosts=[ResolvedHost(host="vm-1", host_id="11094")],
        hypotheses=[
            Hypothesis(
                id="H1",
                statement="운영자가 중지함",
                status="active",
                supporting_evidence_ids=[],
                counter_evidence_ids=[],
                rationale=None,
            )
        ],
        tool_results=[observation],
        tool_call_count=1,
        last_observation=observation,
    )

    node = HypothesisUpdaterNode(
        model=_Model(
            {
                "updates": [
                    update(supporting_evidence_ids=["wazuh:alerts:vm-1:does-not-exist"])
                ],
                "new_hypotheses": [],
                "new_facts": [],
                "stop_reason": None,
            }
        ),
        model_name="test",
    )

    result = await node(state)
    updated = {item.id: item for item in result["hypotheses"]}
    assert updated["H1"].supporting_evidence_ids == []
    assert any(
        item.code == "hypothesis_update_evidence_dropped"
        for item in result["unknowns"]
    )
