"""Conditional edges and deterministic hard-stop policy."""

from datetime import UTC, datetime
from typing import Literal

from aiops_rca.graph.state import InvestigationState


def hard_stop_update(
    state: InvestigationState,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return a state update only when a non-negotiable limit has been reached."""

    if state.fatal_error:
        return {"stop_reason": f"fatal state error: {state.fatal_error}"}
    if not state.hosts:
        return {"stop_reason": "no host could be resolved for investigation"}
    if state.tool_call_count >= state.limits.max_tool_calls:
        return {
            "stop_reason": "maximum tool-call budget reached",
            "limit_reached": True,
        }
    if state.iteration_count >= state.limits.max_iterations:
        return {
            "stop_reason": "maximum investigation iterations reached",
            "limit_reached": True,
        }
    current = now or datetime.now(UTC)
    if state.elapsed_seconds(current) >= state.limits.max_duration_seconds:
        return {
            "stop_reason": "maximum investigation duration reached",
            "limit_reached": True,
        }
    return {}


def route_after_host_resolution(
    state: InvestigationState,
) -> Literal["establish_phenomenon", "evidence_package_builder"]:
    if state.stop_reason or state.fatal_error or not state.hosts:
        return "evidence_package_builder"
    return "establish_phenomenon"


def route_after_stop_guard(
    state: InvestigationState,
) -> Literal["observation_planner", "evidence_package_builder"]:
    return "evidence_package_builder" if state.stop_reason else "observation_planner"


def route_after_tool_router(
    state: InvestigationState,
) -> Literal["tool_executor", "evidence_package_builder"]:
    if state.stop_reason or state.fatal_error or not state.planned_tool_call:
        return "evidence_package_builder"
    return "tool_executor"
