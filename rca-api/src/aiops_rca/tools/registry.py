"""Read-only MCP tool catalog and deterministic routing guards."""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from aiops_rca.schemas.base import StrictModel
from aiops_rca.sources import ToolSource

ToolKind = Literal["structured", "generic", "inventory"]
TemporalScope = Literal["historical", "current_only", "any"]


class ToolPolicyError(ValueError):
    """A planned call violates a deterministic investigation policy.

    `retryable` says whether handing this back to the planner could produce a
    better call. A malformed one could: it named a host nobody resolved or left
    out an argument the server requires, and the message says which.

    A policy refusal could not, and handing one back is worse than useless. The
    refusal names the condition it wants, so the planner reads it as
    instructions -- a live run was told `query_zabbix` needs evidence that the
    structured tools are insufficient, replanned with the permission flag set,
    and spent six of ten calls in the escape hatch. A guard the planner can
    open by being told it is closed is not a guard.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class RoutingContext(StrictModel):
    temporal_scope: Literal["historical", "current", "timeless"] = "timeless"
    generic_fallback_allowed: bool = False
    tool_call_count: Annotated[int, Field(ge=0)] = 0
    max_tool_calls: Annotated[int, Field(ge=1, le=100)] = 30
    #: The query policy the selected report template declares. It is a fact
    #: about the report rather than about the call, and it travels here because
    #: the argument it becomes is applied at the adapter -- which sees the call
    #: and not the template.
    declared_window_policy: str | None = None


class ToolPolicy(StrictModel):
    name: str
    source: ToolSource
    kind: ToolKind = "structured"
    # Arguments without which the call is not worth making. This is a copy of
    # what the servers declare in their own input schemas, and a copy drifts:
    # host_id was listed here as required long after the tools began taking a
    # host name instead, so the pipeline refused calls the server would have
    # answered. How a tool wants to be addressed is the tool's to say.
    requires: tuple[str, ...] = ()
    requires_any: tuple[str, ...] = ()
    priority: int = 100
    blocked_reason: str | None = None
    result_list_fields: tuple[str, ...] = ()
    # Set when the tool takes a query-policy argument that widens its window
    # limit. Named here rather than in a list of tool names so adding a tool
    # cannot leave the widening behind.
    window_policy_argument: str | None = None


class ToolRegistry:
    def __init__(self, policies: Iterable[ToolPolicy]) -> None:
        policy_list = list(policies)
        self._policies = {policy.name: policy for policy in policy_list}
        if len(self._policies) != len(policy_list):
            raise ValueError("tool names must be unique")

    def get(self, name: str) -> ToolPolicy:
        try:
            return self._policies[name]
        except KeyError as error:
            raise ToolPolicyError(f"tool is not allowlisted: {name}") from error

    def list(self) -> tuple[ToolPolicy, ...]:
        return tuple(
            sorted(self._policies.values(), key=lambda item: (item.priority, item.name))
        )

    def validate_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: RoutingContext,
        declared_scope: TemporalScope = "any",
    ) -> ToolPolicy:
        """Whether this call, with these arguments, is allowed to happen.

        `declared_scope` is what the server said about the tool, not what this
        service decided about it. It used to be a field here -- a table of tool
        names on our side describing another server's tools, which went stale
        the moment that server changed and told a pipeline the names of things
        it should not have to know.
        """
        policy = self.get(name)
        if policy.blocked_reason:
            raise ToolPolicyError(
                f"{name} is blocked: {policy.blocked_reason}", retryable=False
            )
        if context.tool_call_count >= context.max_tool_calls:
            raise ToolPolicyError("tool call budget is exhausted", retryable=False)
        if declared_scope == "current_only" and context.temporal_scope == "historical":
            # The one rule the loop cannot recover from. A list of what is
            # running now is a well-formed answer to "what was running
            # yesterday" and nothing downstream can tell it apart from a right
            # one, so it has to be refused before the call rather than judged
            # after it.
            raise ToolPolicyError(
                f"{name} reports current state and cannot prove historical state",
                retryable=False,
            )
        if policy.kind == "generic" and not context.generic_fallback_allowed:
            raise ToolPolicyError(
                f"{name} is generic and requires evidence that structured tools are insufficient",
                # Not retryable: the planner would satisfy it by asserting the
                # permission rather than by finding a structured tool.
                retryable=False,
            )

        missing = [key for key in policy.requires if not _present(arguments.get(key))]
        if missing:
            raise ToolPolicyError(
                f"{name} is missing required arguments: {', '.join(missing)}"
            )
        if policy.requires_any and not any(
            _present(arguments.get(key)) for key in policy.requires_any
        ):
            raise ToolPolicyError(
                f"{name} requires at least one of: {', '.join(policy.requires_any)}",
            )

        _validate_tool_specific_arguments(name, arguments)
        return policy

    def names(self) -> tuple[str, ...]:
        """Every tool the planner may name.

        The planner used to name an effect and a table turned that into a tool.
        Offering the tool names directly is the same guarantee with one
        vocabulary instead of two.
        """
        return tuple(sorted(self._policies))


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _require_digit_id(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.isdigit():
        raise ToolPolicyError(f"{field} must be a decimal string ID")


def _validate_window(arguments: Mapping[str, Any]) -> None:
    if _present(arguments.get("window")):
        window = arguments["window"]
        if (
            not isinstance(window, Mapping)
            or not _present(window.get("from"))
            or not _present(window.get("to"))
        ):
            raise ToolPolicyError("window must contain from and to")


def _validate_tool_specific_arguments(name: str, arguments: Mapping[str, Any]) -> None:
    _validate_window(arguments)
    if name == "find_hosts" and not (
        _present(arguments.get("query")) or _present(arguments.get("group_ids"))
    ):
        raise ToolPolicyError("find_hosts requires query or group_ids")
    if name == "get_metric_summary":
        item_ids = arguments.get("item_ids")
        if not isinstance(item_ids, list) or not 1 <= len(item_ids) <= 20:
            raise ToolPolicyError(
                "get_metric_summary.item_ids must contain 1 to 20 IDs"
            )
        for item_id in item_ids:
            _require_digit_id(item_id, "item_ids[]")
    if name == "get_metric_history":
        item_id = arguments.get("item_id")
        if isinstance(item_id, list):
            raise ToolPolicyError(
                "get_metric_history.item_id must be a scalar, not item_ids"
            )
        _require_digit_id(item_id, "item_id")


def _tool(name: str, source: ToolSource, **kwargs: Any) -> ToolPolicy:
    return ToolPolicy(name=name, source=source, **kwargs)


DEFAULT_TOOL_REGISTRY = ToolRegistry(
    [
        _tool(
            "find_hosts",
            "zabbix",
            requires_any=("query", "group_ids"),
            priority=10,
            result_list_fields=("hosts",),
        ),
        _tool(
            "get_incident_events",
            "zabbix",
            requires=("time_from", "time_to"),
            priority=20,
            result_list_fields=("events",),
            window_policy_argument="policy",
        ),
        _tool(
            "get_trigger_details",
            "zabbix",
            requires=("trigger_id",),
            priority=20,
        ),
        _tool(
            "get_related_events",
            "zabbix",
            requires=("time_from", "time_to"),
            requires_any=("trigger_ids", "tags"),
            priority=25,
            result_list_fields=("events",),
            window_policy_argument="policy",
        ),
        _tool(
            "list_relevant_metrics",
            "zabbix",
            requires=("keywords",),
            priority=20,
            result_list_fields=("metrics",),
        ),
        _tool(
            "get_metric_summary",
            "zabbix",
            requires=("item_ids", "time_from", "time_to", "aggregation"),
            priority=20,
            result_list_fields=("series", "metrics"),
            window_policy_argument="policy",
        ),
        _tool(
            "get_metric_history",
            "zabbix",
            requires=("item_id", "time_from", "time_to", "aggregation"),
            priority=30,
            result_list_fields=("points",),
        ),
        _tool(
            "query_zabbix",
            "zabbix",
            kind="generic",
            requires=("method",),
            priority=90,
            # Whatever object was asked for arrives under one generic name.
            # Without this the rows had nowhere to go but the summary, and a
            # host with twenty-six triggers was reported as twenty-two and a
            # sentence saying the rest was cut.
            result_list_fields=("rows",),
        ),
        _tool(
            "search",
            "elasticsearch",
            kind="generic",
            requires=("index", "query_body"),
            priority=80,
            result_list_fields=("hits",),
        ),
        _tool(
            "esql",
            "elasticsearch",
            kind="generic",
            requires=("query",),
            priority=80,
        ),
        _tool(
            "get_mappings",
            "elasticsearch",
            kind="inventory",
            requires=("index",),
            blocked_reason="known upstream response decoding failure",
            priority=200,
        ),
        _tool(
            "list_indices",
            "elasticsearch",
            kind="inventory",
            requires=("index_pattern",),
            priority=200,
        ),
        _tool(
            "get_shards",
            "elasticsearch",
            kind="inventory",
            priority=200,
        ),
        _tool(
            "get_wazuh_alert_summary",
            "wazuh",
            requires=("time_from", "time_to"),
            priority=20,
            result_list_fields=("alerts",),
        ),
        _tool(
            "get_wazuh_agents",
            "wazuh",
            priority=25,
            result_list_fields=("agents",),
        ),
        _tool(
            "get_wazuh_agent_processes",
            "wazuh",
            requires=("agent_id",),
            priority=30,
            result_list_fields=("processes",),
        ),
        _tool(
            "get_wazuh_agent_ports",
            "wazuh",
            requires=("agent_id", "protocol", "state"),
            priority=30,
            result_list_fields=("ports",),
        ),
    ],
)


# A window this long is refused by every standard policy the MCPs run, so
# widening one can only turn a certain failure into an answer. Windows in the
# hours around the real cap are left exactly as the planner wrote them, where
# guessing wrong would change a working call.
LONG_WINDOW = timedelta(days=2)
LONG_WINDOW_POLICY = "long_term_capacity"


def apply_window_policy(
    policy: ToolPolicy,
    arguments: Mapping[str, Any],
    declared: str | None = None,
) -> dict[str, Any]:
    """Ask for the long-window policy when the report or the window needs it.

    The planner writes the window; the row limits and window caps are the MCP's,
    and it does not have them. A month-long question was reaching the Zabbix MCP
    under the default policy and coming back capped at 26 hours, which the
    report then read as the whole month.

    `declared` is what the report template says in `collection.window.policy`.
    Nothing read it: the span below was the only way this argument was ever
    set, so a template asking for the long-window policy got it by accident of
    its window being long, and would have got nothing had it not been.

    A declaration can only widen. Asking for `long_term_capacity` applies it
    whatever the span; asking for `standard` leaves the span rule in place
    rather than forcing the default back on, because the span rule exists to
    stop a long window being silently capped and a template should not be able
    to opt back into that by saying nothing unusual.
    """
    updated = dict(arguments)
    argument = policy.window_policy_argument
    if not argument or _present(updated.get(argument)):
        return updated
    if declared == LONG_WINDOW_POLICY:
        updated[argument] = LONG_WINDOW_POLICY
        return updated
    span = _window_span(updated)
    if span is not None and span > LONG_WINDOW:
        updated[argument] = LONG_WINDOW_POLICY
    return updated


def _window_span(arguments: Mapping[str, Any]) -> timedelta | None:
    start, end = arguments.get("time_from"), arguments.get("time_to")
    window = arguments.get("window")
    if isinstance(window, Mapping):
        start = start or window.get("from")
        end = end or window.get("to")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        return datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except ValueError:
        # Malformed timestamps are the MCP's to reject, with its own message.
        return None
