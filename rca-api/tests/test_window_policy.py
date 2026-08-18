"""A long window has to ask for the policy that permits it.

A monthly capacity report asked Zabbix for a month of trigger events under the
default policy. The MCP caps that policy at 26 hours, so the reply covered one
day, said nothing about the other twenty-nine, and the report counted the events
of a single day as the events of July.

The planner writes the window. The caps belong to the MCP and are not in the
planner's contract, so the widening is decided here from the window itself.
"""

from aiops_rca.tools.registry import (
    DEFAULT_TOOL_REGISTRY,
    LONG_WINDOW_POLICY,
    apply_window_policy,
)

MONTH = {
    "host_id": "11094",
    "time_from": "2026-07-01T00:00:00+09:00",
    "time_to": "2026-08-01T00:00:00+09:00",
}
HOUR = {
    "host_id": "11094",
    "time_from": "2026-07-01T00:00:00+09:00",
    "time_to": "2026-07-01T01:00:00+09:00",
}


def _policy(name: str):
    return DEFAULT_TOOL_REGISTRY.get(name)


def test_a_month_of_events_asks_for_the_long_window():
    arguments = apply_window_policy(_policy("get_incident_events"), MONTH)
    assert arguments["policy"] == LONG_WINDOW_POLICY


def test_related_events_and_metric_summaries_widen_too():
    for name in ("get_related_events", "get_metric_summary"):
        assert apply_window_policy(_policy(name), MONTH)["policy"] == LONG_WINDOW_POLICY


def test_an_incident_window_is_left_alone():
    # The default policy is what an incident should use: it reads history rather
    # than trends, and widening every call would cost resolution for nothing.
    assert "policy" not in apply_window_policy(_policy("get_incident_events"), HOUR)


def test_an_explicit_choice_is_never_overwritten():
    asked = {**MONTH, "policy": "standard"}
    assert apply_window_policy(_policy("get_incident_events"), asked)["policy"] == "standard"


def test_a_tool_without_the_argument_is_untouched():
    # get_metric_history takes no policy; adding one would be rejected by the
    # MCP's strict input schema and lose the call.
    assert apply_window_policy(_policy("get_metric_history"), MONTH) == MONTH


def test_an_unparseable_window_is_left_for_the_mcp_to_reject():
    broken = {**MONTH, "time_to": "last tuesday"}
    assert "policy" not in apply_window_policy(_policy("get_incident_events"), broken)


def test_the_arguments_are_copied_rather_than_mutated():
    original = dict(MONTH)
    apply_window_policy(_policy("get_incident_events"), original)
    assert original == MONTH


def test_every_tool_that_takes_the_argument_is_a_windowed_one():
    # A tool with no window cannot have its window widened, and marking one
    # would put an argument its schema does not accept into every long call.
    for policy in DEFAULT_TOOL_REGISTRY.list():
        if policy.window_policy_argument:
            assert "time_from" in policy.requires, policy.name
