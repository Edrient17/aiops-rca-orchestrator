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


def route_after_evidence_normalizer(
    state: InvestigationState,
) -> Literal["hypothesis_updater", "stop_guard"]:
    """The judgement, or the end of the run.

    The normalizer records a fatal error when a turn produced no observation to
    normalize, and every other node that can set one is followed by an edge
    that reads it. This one was not: the edge out was unconditional, and the
    updater's first line raises when it is handed no observation. So the error
    left the graph as an exception rather than as state -- a 500 that discarded
    the report, the trace, the audit rows and every tool call already paid for,
    which is the outcome the fatal_error field exists to prevent.

    Sent to the stop guard rather than straight to the package, because
    `hard_stop_update` already turns a fatal error into a stop_reason. The
    investigation ends with an honest account of why instead of a package
    claiming it completed.
    """
    if state.fatal_error or not state.last_observations:
        return "stop_guard"
    return "hypothesis_updater"


def route_after_stop_guard(
    state: InvestigationState,
) -> Literal["observation_planner", "evidence_package_builder"]:
    """Another observation, or the report.

    This used to have a third answer. A section declared the evidence it was
    written from, and a run that stopped with one of those uncollected was sent
    to a sweep that collected it before the report could be written. The
    guarantee was real and so was its cost: the declaration drove collection
    whether the question needed it or not, so a request about templates still
    paid for a process query.

    It is checked after the report is written now -- a required section left
    empty with nothing said about why sends the draft back.
    """
    if not state.stop_reason:
        return "observation_planner"
    return "evidence_package_builder"


def route_after_report_eval(
    state: InvestigationState) -> Literal["__end__"]:
    """Out. The checks record what they found; they no longer send a draft back.

    They were calibrated against a hundred-odd reports from the pipeline as it
    stood months ago, and the pipeline has moved: rows travel in `observed` now,
    a turn asks several questions, the templates are new. Every rejection
    inspected since has been the check being stale rather than the report being
    wrong -- a count read as 261 because it was written 7,261, a number said to
    come from nowhere while it sat in the evidence rows.

    Recording a wrong finding costs a line someone can dismiss. Feeding it back
    changes the report, and the way to satisfy a false finding is to stop
    stating numbers the evidence supports. That asymmetry is why the loop
    closes here while the checks stay on.

    Reopening it needs a measurement rather than an argument, and the thing
    that would supply one -- reports a human has marked correct -- is now being
    collected.
    """
    return "__end__"


def route_after_tool_router(
    state: InvestigationState,
) -> Literal["tool_executor", "observation_planner", "evidence_package_builder"]:
    """The call, another plan, or the report.

    A refused plan used to mean the report: the router set a stop_reason and the
    investigation ended with whatever it had, which in one live run was two tool
    calls and a candidate that named a source where a host belonged. The writer
    has always been allowed a second draft against the reason the first was
    rejected. This is the same turn, one stage earlier.
    """
    if state.fatal_error:
        return "evidence_package_builder"
    if state.planned_tool_calls:
        return "tool_executor"
    if state.stop_reason:
        return "evidence_package_builder"
    if state.routing_rejections:
        return "observation_planner"
    return "evidence_package_builder"
