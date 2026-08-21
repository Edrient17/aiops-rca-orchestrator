"""Independent questions are asked at once.

Measured across four investigations, tool calls were 1-2% of the wall clock and
the model turns around them were the rest: seven calls to Elasticsearch, Zabbix
and Wazuh took two seconds inside a run that took two hundred and seventy-nine.
A cycle is plan, call, judge -- twenty-six seconds of model either side of three
tenths of a second of data -- and a survey that asks four independent things
spent four cycles waiting for permission to ask the next one.

So a turn carries as many questions as do not depend on each other, and they
are executed together.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from conftest import make_state

from aiops_rca.graph.deterministic_nodes import ToolExecutorNode, ToolRouterNode
from aiops_rca.schemas.investigation import (
    Hypothesis,
    ObservationQuestion,
    PlannedToolCall,
    ResolvedHost,
)
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY
from aiops_rca.tools.result import ToolExecutionResult

NOW = datetime(2026, 8, 21, tzinfo=UTC)
HOST = ResolvedHost(host="vm-known", host_id="11094")
H1 = Hypothesis(id="h1", statement="어제 오류가 늘었다")
WINDOW = {"time_from": "2026-08-20T00:00:00Z", "time_to": "2026-08-21T00:00:00Z"}


def ask(tool: str, question: str, **arguments: Any) -> ObservationQuestion:
    return ObservationQuestion(
        question=question,
        discriminates_hypothesis_ids=["h1"],
        temporal_scope="historical",
        required_tool=tool,
        arguments=arguments,
        host="vm-known",
        generic_fallback_allowed=tool in {"esql", "search", "query_zabbix"},
    )


class SlowExecutor:
    """Every call takes the same measurable moment, so waiting shows up."""

    def __init__(self, delay: float = 0.15, fail: set[str] | None = None) -> None:
        self.delay = delay
        self.fail = fail or set()
        self.started: list[str] = []

    async def execute(self, planned: PlannedToolCall, _context: Any):
        self.started.append(planned.purpose)
        await asyncio.sleep(self.delay)
        if planned.tool_name in self.fail:
            raise RuntimeError(f"{planned.tool_name} exploded")
        return ToolExecutionResult(
            tool_call_id=f"call-{planned.purpose}",
            tool_name=planned.tool_name,
            source="elasticsearch" if planned.tool_name == "esql" else "zabbix",
            status="ok",
            request=dict(planned.arguments),
            response={"rows": [1, 2, 3]},
            started_at=NOW,
            finished_at=NOW,
        )


THREE = [
    ask("esql", "시간당 물량", query="FROM vm-logs-* | STATS n = COUNT(*)"),
    ask("esql", "서비스별 물량", query="FROM vm-logs-* | STATS n = COUNT(*) BY x"),
    ask("get_incident_events", "이벤트가 있었는가", host="vm-known", **WINDOW),
]


def _route(questions):
    state = make_state(hosts=[HOST], hypotheses=[H1], next_questions=questions)
    return dict(asyncio.run(ToolRouterNode(DEFAULT_TOOL_REGISTRY)(state)))


def _execute(planned, questions, executor):
    state = make_state(
        hosts=[HOST],
        hypotheses=[H1],
        next_questions=questions,
        planned_tool_calls=planned,
    )
    return dict(asyncio.run(ToolExecutorNode(executor)(state)))


class TestRoutingAWholeTurn:
    def test_every_question_becomes_its_own_call(self):
        update = _route(THREE)
        assert [call.tool_name for call in update["planned_tool_calls"]] == [
            "esql",
            "esql",
            "get_incident_events",
        ]

    def test_two_questions_of_the_same_tool_stay_apart(self):
        # They were keyed by tool name, so the second overwrote the first and a
        # survey could not ask for volume by hour and volume by service.
        update = _route(THREE)
        queries = [
            call.arguments["query"]
            for call in update["planned_tool_calls"]
            if call.tool_name == "esql"
        ]
        assert len(set(queries)) == 2

    def test_the_purpose_travels_with_the_call_it_belongs_to(self):
        update = _route(THREE)
        assert [call.purpose for call in update["planned_tool_calls"]] == [
            "시간당 물량",
            "서비스별 물량",
            "이벤트가 있었는가",
        ]

    def test_one_refused_question_does_not_cost_the_others(self):
        refused = ask("get_incident_events", "인자가 빠진 질문")
        update = _route([*THREE, refused])
        assert len(update["planned_tool_calls"]) == 3
        assert "tool_routing_blocked" in [i.code for i in update["unknowns"]]


class TestMakingTheCallsTogether:
    def test_they_do_not_wait_for_each_other(self):
        executor = SlowExecutor(delay=0.15)
        planned = _route(THREE)["planned_tool_calls"]
        began = datetime.now(UTC)
        update = _execute(planned, THREE, executor)
        elapsed = (datetime.now(UTC) - began).total_seconds()
        assert len(update["last_observations"]) == 3
        # Three 0.15s calls in sequence is 0.45s. Together they are one of them.
        assert elapsed < 0.35, f"took {elapsed:.2f}s, which is one after another"

    def test_every_answer_is_kept_and_counted(self):
        planned = _route(THREE)["planned_tool_calls"]
        update = _execute(planned, THREE, SlowExecutor(delay=0.01))
        assert update["tool_call_count"] == 3
        assert len(update["tool_results"]) == 3
        assert len(update["tool_call_purposes"]) == 3

    def test_one_call_raising_does_not_discard_the_others(self):
        # They already ran. Their answers are paid for.
        planned = _route(THREE)["planned_tool_calls"]
        executor = SlowExecutor(delay=0.01, fail={"get_incident_events"})
        update = _execute(planned, THREE, executor)
        assert len(update["last_observations"]) == 2
        codes = [item.code for item in update["unknowns"]]
        assert "tool_call_failed" in codes

    def test_the_failure_says_what_it_was(self):
        planned = _route(THREE)["planned_tool_calls"]
        executor = SlowExecutor(delay=0.01, fail={"get_incident_events"})
        update = _execute(planned, THREE, executor)
        message = next(
            item.message
            for item in update["unknowns"]
            if item.code == "tool_call_failed"
        )
        assert "exploded" in message


class TestTheBudgetAcrossABatch:
    def test_a_turn_cannot_outspend_what_is_left(self):
        # Four calls planned together still spend four, so the count has to
        # advance inside the batch rather than after it.
        from aiops_rca.schemas.investigation import InvestigationLimits

        state = make_state(
            hosts=[HOST],
            hypotheses=[H1],
            next_questions=THREE,
            limits=InvestigationLimits(max_tool_calls=2),
        )
        update = dict(asyncio.run(ToolRouterNode(DEFAULT_TOOL_REGISTRY)(state)))
        assert len(update["planned_tool_calls"]) == 2
        assert any(
            "budget" in item.message for item in update["unknowns"]
        )


def test_a_generic_tool_still_needs_its_gate_opened_per_question():
    # The gate is per question, so a batch holding one that needs the escape
    # hatch and one that does not cannot grant it to both.
    closed = ask("query_zabbix", "게이트 없이", method="host.get").model_copy(
        update={"generic_fallback_allowed": False},
    )
    update = _route([closed, THREE[0]])
    assert [call.tool_name for call in update["planned_tool_calls"]] == ["esql"]
    assert "tool_routing_blocked" in [item.code for item in update["unknowns"]]
