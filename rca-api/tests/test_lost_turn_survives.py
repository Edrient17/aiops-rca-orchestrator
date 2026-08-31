"""A turn that collects nothing ends the run; it does not end the request.

Three separate mistakes met here, and each of them discarded a whole
investigation -- the report, the trace, the audit rows and every tool call
already paid for -- by raising out of the graph as an HTTP 500. ingress reads
that as a failed delivery and re-runs the same investigation up to nine times,
so the cost of each was multiplied by nine.

They are tested together because they were one failure in practice: the batch
executed under a context the router never granted it, every call in it was
refused, and the node reached with nothing to normalize had nowhere to send
that fact.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from conftest import make_state

from aiops_rca.graph.builder import CollectorNodes, build_collector_graph
from aiops_rca.graph.deterministic_nodes import (
    EvidenceNormalizerNode,
    ToolExecutorNode,
    ToolRouterNode,
)
from aiops_rca.graph.routing import route_after_evidence_normalizer
from aiops_rca.graph.state import (
    CAPACITY_NOTICE,
    KNOWN_FACTS_CAPACITY,
    UNKNOWNS_CAPACITY,
    InvestigationState,
)
from aiops_rca.schemas.evidence_package import Evidence
from aiops_rca.schemas.investigation import (
    Hypothesis,
    KnownFact,
    ObservationQuestion,
    ResolvedHost,
    UnknownItem,
)
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY
from aiops_rca.tools.result import ToolExecutionResult

NOW = datetime(2026, 8, 21, tzinfo=UTC)
HOST = ResolvedHost(host="vm-known", host_id="11094")
H1 = Hypothesis(id="h1", statement="어제 오류가 늘었다")
#: Something for a confirmed fact to be about; the state refuses one that
#: cites evidence it does not hold.
EVIDENCE = Evidence.model_validate(
    {
        "evidence_id": "zbx:event:1",
        "evidence_type": "event",
        "source": "zabbix",
        "summary": "problem event",
        "observed_at": None,
        "window": None,
        "resource_ids": {
            "host_id": "11094",
            "event_id": "1",
            "trigger_id": None,
            "item_id": None,
        },
        "metric": None,
        "data_quality": None,
        "tool_call_id": "call-seed",
    },
)


def ask(question: str, *, scope: str = "historical", gate: bool = True):
    return ObservationQuestion(
        question=question,
        discriminates_hypothesis_ids=["h1"],
        temporal_scope=scope,
        required_tool="esql",
        arguments={"query": question},
        host="vm-known",
        generic_fallback_allowed=gate,
    )


class Recording:
    """Validates the way the adapter does, and remembers what it was given."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, bool]] = []

    async def execute(self, planned: Any, context: Any) -> ToolExecutionResult:
        DEFAULT_TOOL_REGISTRY.validate_call(
            planned.tool_name, planned.arguments, context
        )
        self.seen.append(
            (planned.purpose, context.temporal_scope, context.generic_fallback_allowed)
        )
        return ToolExecutionResult(
            tool_call_id=f"call-{planned.purpose}",
            tool_name=planned.tool_name,
            source="elasticsearch",
            status="ok",
            request=dict(planned.arguments),
            response={"rows": [1]},
            started_at=NOW,
            finished_at=NOW,
        )


def _route(questions):
    state = make_state(hosts=[HOST], hypotheses=[H1], next_questions=questions)
    return dict(asyncio.run(ToolRouterNode(DEFAULT_TOOL_REGISTRY)(state)))


def _execute(questions, planned, executor):
    state = make_state(
        hosts=[HOST],
        hypotheses=[H1],
        next_questions=questions,
        planned_tool_calls=planned,
    )
    return dict(asyncio.run(ToolExecutorNode(executor)(state)))


class TestTheContextEachCallRunsUnder:
    """The executor rebuilt it from a dict keyed by tool name.

    Two questions of the same tool are the ordinary shape of a turn --
    `observation_planner.md` asks for exactly that -- and keying by name kept
    one entry for both. The adapter validates a second time, so the call ran
    under a context the router never granted it and was refused.
    """

    def test_two_questions_of_one_tool_keep_their_own_scope(self):
        questions = [
            ask("지금 무엇이 도는가", scope="current"),
            ask("어젯밤 무엇이 돌았는가", scope="historical"),
        ]
        executor = Recording()
        _execute(questions, _route(questions)["planned_tool_calls"], executor)
        assert sorted(scope for _, scope, _ in executor.seen) == [
            "current",
            "historical",
        ]

    def test_a_refused_question_does_not_close_an_allowed_one_s_gate(self):
        # The refused question stays in next_questions, so its gate overwrote
        # the entry for the question the router had already allowed -- and the
        # whole turn came back empty.
        questions = [ask("근거가 있는 질문"), ask("게이트 없는 질문", gate=False)]
        routed = _route(questions)
        assert [call.purpose for call in routed["planned_tool_calls"]] == [
            "근거가 있는 질문"
        ]

        executor = Recording()
        update = _execute(questions, routed["planned_tool_calls"], executor)
        assert [gate for _, _, gate in executor.seen] == [True]
        assert len(update["last_observations"]) == 1
        assert [item.code for item in update["unknowns"]] == []


class TestWhereAnEmptyTurnGoes:
    """The normalizer records a fatal error. Something has to read it."""

    def test_it_is_sent_to_the_stop_guard_rather_than_to_the_updater(self):
        state = make_state(
            hosts=[HOST],
            hypotheses=[H1],
            fatal_error="evidence_normalizer entered without an observation and plan",
        )
        assert route_after_evidence_normalizer(state) == "stop_guard"

    def test_an_ordinary_turn_still_reaches_the_updater(self):
        state = make_state(
            hosts=[HOST],
            hypotheses=[H1],
            last_observations=[
                ToolExecutionResult(
                    tool_call_id="call-1",
                    tool_name="esql",
                    source="elasticsearch",
                    status="ok",
                    request={},
                    response={"rows": [1]},
                    started_at=NOW,
                    finished_at=NOW,
                )
            ],
        )
        assert route_after_evidence_normalizer(state) == "hypothesis_updater"

    def test_the_graph_writes_a_report_instead_of_raising(self):
        """End to end: every call in a turn refused, and the run still answers.

        The updater raises when handed no observation, and the edge into it
        used to be unconditional -- so this exact path left the graph as an
        exception and returned a 500.
        """
        reached: list[str] = []

        async def resolve_hosts(state):
            return {"hosts": [HOST], "visited_nodes": [*state.visited_nodes, "resolve_hosts"]}

        async def establish_phenomenon(state):
            return {"phenomenon": "오류가 늘었다", "visited_nodes": [*state.visited_nodes]}

        async def hypothesis_planner(state):
            return {"hypotheses": [H1], "visited_nodes": [*state.visited_nodes]}

        async def observation_planner(state):
            return {
                "next_questions": [ask("한 번만 묻는다")],
                "planned_tool_calls": [],
                "iteration_count": state.iteration_count + 1,
                "visited_nodes": [*state.visited_nodes],
            }

        async def refusing_executor_node(state):
            # What ToolExecutorNode produces when every call in the batch is
            # refused before it reaches a transport.
            return {
                "last_observations": [],
                "unknowns": [
                    *state.unknowns,
                    UnknownItem(code="tool_call_failed", message="esql: refused"),
                ],
                "visited_nodes": [*state.visited_nodes, "tool_executor"],
            }

        async def updater(state):
            reached.append("hypothesis_updater")
            raise AssertionError("the updater must not be entered without an observation")

        async def package(state):
            return {
                "evidence_package": None,
                "visited_nodes": [*state.visited_nodes, "evidence_package_builder"],
            }

        async def passthrough(state):
            return {}

        from aiops_rca.graph.deterministic_nodes import StopGuardNode

        graph = build_collector_graph(
            CollectorNodes(
                resolve_hosts=resolve_hosts,
                establish_phenomenon=establish_phenomenon,
                hypothesis_planner=hypothesis_planner,
                observation_planner=observation_planner,
                tool_router=ToolRouterNode(DEFAULT_TOOL_REGISTRY),
                tool_executor=refusing_executor_node,
                evidence_normalizer=EvidenceNormalizerNode(),
                hypothesis_updater=updater,
                stop_guard=StopGuardNode(),
                evidence_package_builder=package,
                report_writer=passthrough,
                report_eval=passthrough,
            ),
        )

        finished = InvestigationState.model_validate(
            asyncio.run(graph.ainvoke(make_state()))
        )
        assert reached == []
        assert finished.stop_reason is not None
        assert "fatal state error" in finished.stop_reason
        assert "evidence_package_builder" in finished.visited_nodes


class TestTheCeilingOnWhatOnlyGrows:
    """Thirty-odd sites append an unknown and none of them prunes.

    The ceiling was enforced by the model and nowhere else, so the item that
    crossed it raised out of the graph -- discarding a finished investigation
    over the last line of a list whose whole purpose is to record what went
    wrong. `merge_evidence` already stops collection at its own ceiling for
    this reason.
    """

    def _unknowns(self, count: int) -> list[UnknownItem]:
        return [UnknownItem(code="tool_error", message=f"failure {i}") for i in range(count)]

    def test_more_unknowns_than_the_state_holds_does_not_raise(self):
        state = make_state(unknowns=self._unknowns(UNKNOWNS_CAPACITY + 40))
        assert len(state.unknowns) == UNKNOWNS_CAPACITY

    def test_the_trim_says_that_it_trimmed(self):
        state = make_state(unknowns=self._unknowns(UNKNOWNS_CAPACITY + 40))
        last = state.unknowns[-1]
        assert last.code == CAPACITY_NOTICE
        assert "unknowns" in last.message

    def test_what_was_recorded_first_is_what_is_kept(self):
        state = make_state(unknowns=self._unknowns(UNKNOWNS_CAPACITY + 40))
        assert state.unknowns[0].message == "failure 0"

    def test_rebuilding_a_trimmed_state_changes_nothing(self):
        """This state is rebuilt at every node, so the trim has to settle.

        Counting the notice as an ordinary unknown pushed one real entry out
        of the list on each transition, and rewrote the notice to describe only
        that one -- a run losing its record of what went wrong, one line per
        node, while the notice said it had lost one.
        """
        once = make_state(unknowns=self._unknowns(UNKNOWNS_CAPACITY + 40))
        twice = InvestigationState.model_validate(once.model_dump())
        thrice = InvestigationState.model_validate(twice.model_dump())

        assert len(thrice.unknowns) == UNKNOWNS_CAPACITY
        assert [item.message for item in thrice.unknowns] == [
            item.message for item in once.unknowns
        ]
        assert [item.code for item in thrice.unknowns].count(CAPACITY_NOTICE) == 1

    def test_confirmed_facts_are_bounded_too(self):
        # The updater may add twenty a turn, and the loop runs up to ten.
        facts = [
            KnownFact(fact=f"fact {i}", evidence_ids=["zbx:event:1"])
            for i in range(KNOWN_FACTS_CAPACITY + 5)
        ]
        state = make_state(
            evidence=[EVIDENCE],
            known_facts=facts,
            unknowns=[],
        )
        assert len(state.known_facts) == KNOWN_FACTS_CAPACITY
        assert state.unknowns[-1].code == CAPACITY_NOTICE
        assert "confirmed facts" in state.unknowns[-1].message

    def test_a_state_within_its_bounds_is_left_exactly_as_it_was(self):
        state = make_state(unknowns=self._unknowns(3))
        assert [item.code for item in state.unknowns] == ["tool_error"] * 3
