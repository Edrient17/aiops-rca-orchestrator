import asyncio
from datetime import UTC, datetime

from conftest import make_state
from langgraph.checkpoint.memory import InMemorySaver

from aiops_rca.graph.builder import CollectorNodes, build_collector_graph
from aiops_rca.schemas.investigation import (
    Hypothesis,
    ObservationQuestion,
    PlannedToolCall,
    ResolvedHost,
)
from aiops_rca.tools.result import ToolExecutionResult


def test_collector_graph_exposes_and_checkpoints_each_reasoning_boundary():
    now = datetime(2026, 8, 12, 3, tzinfo=UTC)

    async def resolve_hosts(state):
        return {
            "hosts": [ResolvedHost(host="vm-java-docker-2", host_id="11094")],
            "visited_nodes": [*state.visited_nodes, "resolve_hosts"],
        }

    async def establish_phenomenon(state):
        return {
            "phenomenon": "payment-service stopped during the incident window",
            "visited_nodes": [*state.visited_nodes, "establish_phenomenon"],
        }

    async def coverage_sweep(state):
        return {"visited_nodes": [*state.visited_nodes, "coverage_sweep"]}

    async def hypothesis_planner(state):
        return {
            "hypotheses": [
                Hypothesis(id="h1", statement="resource exhaustion"),
                Hypothesis(id="h2", statement="operator stop"),
            ],
            "visited_nodes": [*state.visited_nodes, "hypothesis_planner"],
        }

    async def observation_planner(state):
        return {
            "next_question": ObservationQuestion(
                question="Was a stop command executed before the service stopped?",
                discriminates_hypothesis_ids=["h1", "h2"],
                temporal_scope="historical",
                required_effect="audit_command",
            ),
            "iteration_count": state.iteration_count + 1,
            "visited_nodes": [*state.visited_nodes, "observation_planner"],
        }

    async def tool_router(state):
        return {
            "planned_tool_call": PlannedToolCall(
                tool_name="get_wazuh_alert_summary",
                arguments={
                    "time_from": "2026-08-12T02:00:00Z",
                    "time_to": "2026-08-12T03:00:00Z",
                },
                purpose=state.next_question.question,
                target_hypothesis_ids=state.next_question.discriminates_hypothesis_ids,
            ),
            "visited_nodes": [*state.visited_nodes, "tool_router"],
        }

    async def tool_executor(state):
        result = ToolExecutionResult(
            tool_call_id="call-1",
            tool_name="get_wazuh_alert_summary",
            source="wazuh",
            status="ok",
            request=state.planned_tool_call.arguments,
            response={"alerts": [{"command": "systemctl stop payment-service"}]},
            started_at=now,
            finished_at=now,
        )
        return {
            "last_observation": result,
            "tool_results": [result],
            "tool_call_count": 1,
            "visited_nodes": [*state.visited_nodes, "tool_executor"],
        }

    async def evidence_normalizer(state):
        return {"visited_nodes": [*state.visited_nodes, "evidence_normalizer"]}

    async def hypothesis_updater(state):
        hypotheses = [
            state.hypotheses[0].model_copy(update={"status": "rejected"}),
            state.hypotheses[1].model_copy(update={"status": "supported"}),
        ]
        return {
            "hypotheses": hypotheses,
            "stop_reason": "one explanation is supported and its competitor is rejected",
            "visited_nodes": [*state.visited_nodes, "hypothesis_updater"],
        }

    async def stop_guard(state):
        return {"visited_nodes": [*state.visited_nodes, "stop_guard"]}

    async def evidence_package_builder(state):
        return {"visited_nodes": [*state.visited_nodes, "evidence_package_builder"]}

    saver = InMemorySaver()
    graph = build_collector_graph(
        CollectorNodes(
            resolve_hosts=resolve_hosts,
            establish_phenomenon=establish_phenomenon,
            coverage_sweep=coverage_sweep,
            hypothesis_planner=hypothesis_planner,
            observation_planner=observation_planner,
            tool_router=tool_router,
            tool_executor=tool_executor,
            evidence_normalizer=evidence_normalizer,
            hypothesis_updater=hypothesis_updater,
            stop_guard=stop_guard,
            evidence_package_builder=evidence_package_builder,
        ),
        checkpointer=saver,
    )
    config = {"configurable": {"thread_id": "inv-1"}}
    output = asyncio.run(graph.ainvoke(make_state(), config=config))

    assert output["visited_nodes"] == [
        "resolve_hosts",
        "establish_phenomenon",
        "coverage_sweep",
        "hypothesis_planner",
        "observation_planner",
        "tool_router",
        "tool_executor",
        "evidence_normalizer",
        "hypothesis_updater",
        "stop_guard",
        "evidence_package_builder",
    ]
    assert output["stop_reason"].startswith("one explanation")
    assert len(list(graph.get_state_history(config))) >= 10


def test_no_resolved_host_skips_every_reasoning_node():
    async def resolve_hosts(state):
        return {
            "stop_reason": "no host could be resolved for investigation",
            "visited_nodes": ["resolve_hosts"],
        }

    async def should_not_run(_state):
        raise AssertionError("reasoning node ran without a resolved host")

    async def package(state):
        return {"visited_nodes": [*state.visited_nodes, "evidence_package_builder"]}

    graph = build_collector_graph(
        CollectorNodes(
            resolve_hosts=resolve_hosts,
            establish_phenomenon=should_not_run,
            coverage_sweep=should_not_run,
            hypothesis_planner=should_not_run,
            observation_planner=should_not_run,
            tool_router=should_not_run,
            tool_executor=should_not_run,
            evidence_normalizer=should_not_run,
            hypothesis_updater=should_not_run,
            stop_guard=should_not_run,
            evidence_package_builder=package,
        ),
    )
    output = asyncio.run(graph.ainvoke(make_state()))
    assert output["visited_nodes"] == ["resolve_hosts", "evidence_package_builder"]
