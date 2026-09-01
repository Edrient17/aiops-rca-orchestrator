"""What a template's `collection` block asks for, and what reads it.

Four of its fields had no reader in this service. `host_selector`, `limits`
and `window.range` were honoured; `window.policy`, `guidance`,
`metric_keywords` and `aggregation` were not -- and the orchestrator README
documents two of them as the way an operator steers collection. A template
tuned over a week changed nothing, and nothing said so.

`aggregation` was the sharpest of the four: it is a required argument of the
metric tools, the template names it, and the planner that has to pass it was
never told.
"""

import asyncio
from typing import Any

from conftest import make_state

from aiops_rca.graph.deterministic_nodes import ToolRouterNode
from aiops_rca.graph.live_nodes import ObservationPlannerNode, _collection_brief
from aiops_rca.schemas.investigation import (
    Hypothesis,
    ObservationQuestion,
    ResolvedHost,
)
from aiops_rca.tools.registry import (
    DEFAULT_TOOL_REGISTRY,
    LONG_WINDOW_POLICY,
    RoutingContext,
    apply_window_policy,
)

HOST = ResolvedHost(host="vm-known", host_id="11094")
H1 = Hypothesis(id="h1", statement="용량이 늘고 있다")

COLLECTION: dict[str, Any] = {
    "host_selector": {"mode": "from_question"},
    "window": {"policy": "long_term_capacity", "range": "last_calendar_month"},
    "aggregation": "1d",
    "metric_keywords": ["disk", "filesystem", "cpu"],
    "guidance": "월간 보고서는 호스트 그룹 전체의 추세를 본다.",
    "limits": {"max_tool_calls": 120},
    "resolved_window": {"from": "2026-07-01T00:00:00Z", "to": "2026-08-01T00:00:00Z"},
}


class TestWhatThePlannerIsTold:
    """The whole `collection` object was sent and no prompt named it.

    An operator writing `guidance` was writing into a field that reached the
    model as an unlabelled blob no instruction referred to.
    """

    def test_the_guidance_reaches_the_planner(self):
        brief = _collection_brief(make_state(collection=COLLECTION))
        assert brief["guidance"] == COLLECTION["guidance"]

    def test_the_metric_keywords_reach_the_planner(self):
        brief = _collection_brief(make_state(collection=COLLECTION))
        assert brief["metric_keywords"] == ["disk", "filesystem", "cpu"]

    def test_the_aggregation_reaches_the_planner(self):
        # A required argument of the metric tools, named by the template.
        brief = _collection_brief(make_state(collection=COLLECTION))
        assert brief["aggregation"] == "1d"

    def test_a_template_that_says_none_of_it_is_still_readable(self):
        brief = _collection_brief(make_state(collection={"resolved_window": {}}))
        assert brief == {"guidance": None, "metric_keywords": [], "aggregation": None}

    def test_the_prompt_names_every_field_the_brief_carries(self):
        """The half that made the old blob useless.

        Sending the fields is not the fix on its own -- they were already being
        sent. The fix is that the prompt refers to them, so this asserts the
        two halves agree rather than only the half in Python.
        """
        from importlib.resources import files

        prompt = (files("aiops_rca.prompts") / "observation_planner.md").read_text(
            encoding="utf-8"
        )
        assert "report_collection" in prompt
        for field in _collection_brief(make_state(collection=COLLECTION)):
            assert f"`{field}`" in prompt, f"the prompt never mentions {field}"

    def test_the_planner_payload_carries_it(self):
        sent: dict[str, Any] = {}

        class Recording:
            async def complete(self, **kwargs):
                sent.update(kwargs["payload"])
                raise _Stop

        class _Stop(Exception):
            pass

        node = ObservationPlannerNode(
            model=Recording(), model_name="m", registry=DEFAULT_TOOL_REGISTRY
        )
        state = make_state(hosts=[HOST], hypotheses=[H1], collection=COLLECTION)
        try:
            asyncio.run(node(state))
        except _Stop:
            pass
        assert sent["report_collection"]["aggregation"] == "1d"
        assert sent["report_collection"]["guidance"] == COLLECTION["guidance"]


class TestTheDeclaredWindowPolicy:
    """`collection.window.policy` had no reader at all.

    The monthly capacity report declares `long_term_capacity`, and got it only
    because its window happened to be longer than the span rule's two days. A
    template asking for it over a shorter window would have been ignored.
    """

    def test_the_state_reads_it_from_the_template(self):
        state = make_state(collection=COLLECTION)
        assert state.declared_window_policy == "long_term_capacity"

    def test_a_template_that_declares_nothing_reads_as_nothing(self):
        assert make_state(collection={}).declared_window_policy is None
        assert make_state().declared_window_policy is None

    def test_a_declaration_applies_even_when_the_window_is_short(self):
        # The span rule would not fire here. The template asked anyway.
        policy = DEFAULT_TOOL_REGISTRY.get("get_incident_events")
        arguments = {
            "time_from": "2026-08-20T00:00:00Z",
            "time_to": "2026-08-20T06:00:00Z",
        }
        assert apply_window_policy(policy, arguments) == arguments
        widened = apply_window_policy(policy, arguments, LONG_WINDOW_POLICY)
        assert widened["policy"] == LONG_WINDOW_POLICY

    def test_declaring_standard_does_not_take_the_span_rule_away(self):
        # Otherwise a template could opt back into a month capped at 26 hours
        # by saying nothing unusual.
        policy = DEFAULT_TOOL_REGISTRY.get("get_incident_events")
        arguments = {
            "time_from": "2026-07-01T00:00:00Z",
            "time_to": "2026-08-01T00:00:00Z",
        }
        widened = apply_window_policy(policy, arguments, "standard")
        assert widened["policy"] == LONG_WINDOW_POLICY

    def test_what_the_planner_wrote_is_never_overwritten(self):
        policy = DEFAULT_TOOL_REGISTRY.get("get_incident_events")
        arguments = {
            "time_from": "2026-08-20T00:00:00Z",
            "time_to": "2026-08-20T06:00:00Z",
            "policy": "something_the_planner_chose",
        }
        widened = apply_window_policy(policy, arguments, LONG_WINDOW_POLICY)
        assert widened["policy"] == "something_the_planner_chose"

    def test_the_router_puts_it_on_the_context_it_builds(self):
        question = ObservationQuestion(
            question="지난달 이벤트",
            discriminates_hypothesis_ids=["h1"],
            temporal_scope="historical",
            required_tool="get_incident_events",
            arguments={
                "time_from": "2026-07-01T00:00:00Z",
                "time_to": "2026-08-01T00:00:00Z",
            },
            host="vm-known",
        )
        state = make_state(
            hosts=[HOST],
            hypotheses=[H1],
            collection=COLLECTION,
            next_questions=[question],
        )
        assert state.declared_window_policy == LONG_WINDOW_POLICY
        update = dict(asyncio.run(ToolRouterNode(DEFAULT_TOOL_REGISTRY)(state)))
        assert len(update["planned_tool_calls"]) == 1

    def test_the_context_carries_it_to_the_adapter(self):
        # RoutingContext is where the template's declaration reaches the layer
        # that turns it into an argument, because the adapter sees the call and
        # not the template.
        context = RoutingContext(declared_window_policy=LONG_WINDOW_POLICY)
        assert context.declared_window_policy == LONG_WINDOW_POLICY
        assert RoutingContext().declared_window_policy is None
