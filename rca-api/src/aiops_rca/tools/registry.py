"""Read-only MCP tool catalog and deterministic routing guards."""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from aiops_rca.schemas.base import StrictModel

ToolSource = Literal["zabbix", "elasticsearch", "wazuh"]
ToolKind = Literal["structured", "generic", "inventory"]
TemporalScope = Literal["historical", "current_only", "any"]


class ToolPolicyError(ValueError):
    """A planned call violates a deterministic investigation policy."""


class RoutingContext(StrictModel):
    temporal_scope: Literal["historical", "current", "timeless"] = "timeless"
    generic_fallback_allowed: bool = False
    tool_call_count: Annotated[int, Field(ge=0)] = 0
    max_tool_calls: Annotated[int, Field(ge=1, le=100)] = 30


class ToolPolicy(StrictModel):
    name: str
    source: ToolSource
    kind: ToolKind = "structured"
    requires: tuple[str, ...] = ()
    requires_any: tuple[str, ...] = ()
    effects: tuple[str, ...]
    temporal_scope: TemporalScope = "any"
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
    ) -> ToolPolicy:
        policy = self.get(name)
        if policy.blocked_reason:
            raise ToolPolicyError(f"{name} is blocked: {policy.blocked_reason}")
        if context.tool_call_count >= context.max_tool_calls:
            raise ToolPolicyError("tool call budget is exhausted")
        if (
            policy.temporal_scope == "current_only"
            and context.temporal_scope == "historical"
        ):
            raise ToolPolicyError(
                f"{name} reports current state and cannot prove historical state"
            )
        if policy.kind == "generic" and not context.generic_fallback_allowed:
            raise ToolPolicyError(
                f"{name} is generic and requires evidence that structured tools are insufficient",
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

    def effects(self) -> tuple[str, ...]:
        """Every effect some allowlisted tool can produce.

        route_effect matches an effect exactly, so a planner that writes
        "related_events around the target window" instead of "related_events"
        routes to nothing and the investigation stops with a stop_reason that
        reads like a missing capability. Offering this list as the only
        permitted values turns that into an impossibility.
        """
        return tuple(
            sorted({effect for policy in self._policies.values() for effect in policy.effects})
        )

    def route_effect(
        self,
        effect: str,
        arguments_by_tool: Mapping[str, Mapping[str, Any]],
        context: RoutingContext,
    ) -> ToolPolicy:
        candidates = [policy for policy in self.list() if effect in policy.effects]
        failures: list[str] = []
        for policy in candidates:
            arguments = arguments_by_tool.get(policy.name)
            if arguments is None:
                continue
            try:
                return self.validate_call(policy.name, arguments, context)
            except ToolPolicyError as error:
                failures.append(str(error))
        detail = f" ({'; '.join(failures)})" if failures else ""
        raise ToolPolicyError(f"no allowed tool can produce effect {effect!r}{detail}")


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
    if name in {
        "get_incident_events",
        "get_metric_summary",
        "get_metric_history",
        "list_relevant_metrics",
        "get_related_events",
    }:
        _require_digit_id(arguments.get("host_id"), "host_id")


def _tool(
    name: str,
    source: ToolSource,
    effects: tuple[str, ...],
    **kwargs: Any,
) -> ToolPolicy:
    return ToolPolicy(name=name, source=source, effects=effects, **kwargs)


DEFAULT_TOOL_REGISTRY = ToolRegistry(
    [
        _tool(
            "find_hosts",
            "zabbix",
            ("host_resolution",),
            requires_any=("query", "group_ids"),
            priority=10,
            result_list_fields=("hosts",),
        ),
        _tool(
            "get_incident_events",
            "zabbix",
            ("incident_events", "trigger_anchor"),
            requires=("host_id", "time_from", "time_to"),
            priority=20,
            result_list_fields=("events",),
            window_policy_argument="policy",
        ),
        _tool(
            "get_trigger_details",
            "zabbix",
            ("trigger_definition", "dependency", "linked_items"),
            requires=("trigger_id",),
            priority=20,
        ),
        _tool(
            "get_related_events",
            "zabbix",
            ("related_events",),
            requires=("host_id", "time_from", "time_to"),
            requires_any=("trigger_ids", "tags"),
            priority=25,
            result_list_fields=("events",),
            window_policy_argument="policy",
        ),
        _tool(
            "list_relevant_metrics",
            "zabbix",
            ("metric_candidates",),
            requires=("host_id", "keywords"),
            priority=20,
            result_list_fields=("metrics",),
        ),
        _tool(
            "get_metric_summary",
            "zabbix",
            ("metric_level", "metric_change", "metric_trend"),
            requires=("host_id", "item_ids", "time_from", "time_to", "aggregation"),
            priority=20,
            result_list_fields=("series", "metrics"),
            window_policy_argument="policy",
        ),
        _tool(
            "get_metric_history",
            "zabbix",
            ("metric_temporal_shape",),
            requires=("host_id", "item_id", "time_from", "time_to", "aggregation"),
            priority=30,
            result_list_fields=("points",),
        ),
        _tool(
            "query_zabbix",
            "zabbix",
            ("generic_zabbix_object",),
            kind="generic",
            requires=("method",),
            priority=90,
        ),
        _tool(
            "search",
            "elasticsearch",
            ("raw_log_evidence", "generic_elasticsearch_query"),
            kind="generic",
            requires=("index", "query_body"),
            priority=80,
            result_list_fields=("hits",),
        ),
        _tool(
            "esql",
            "elasticsearch",
            ("long_term_log_baseline", "generic_elasticsearch_query"),
            kind="generic",
            requires=("query",),
            priority=80,
        ),
        _tool(
            "get_mappings",
            "elasticsearch",
            ("index_mapping",),
            kind="inventory",
            requires=("index",),
            blocked_reason="known upstream response decoding failure",
            priority=200,
        ),
        _tool(
            "list_indices",
            "elasticsearch",
            ("index_inventory",),
            kind="inventory",
            requires=("index_pattern",),
            priority=200,
        ),
        _tool(
            "get_shards",
            "elasticsearch",
            ("shard_inventory",),
            kind="inventory",
            priority=200,
        ),
        _tool(
            "get_wazuh_alert_summary",
            "wazuh",
            ("audit_actor", "audit_command"),
            requires=("time_from", "time_to"),
            priority=20,
            result_list_fields=("alerts",),
        ),
        _tool(
            "get_wazuh_agents",
            "wazuh",
            ("audit_coverage", "agent_status"),
            priority=25,
            result_list_fields=("agents",),
        ),
        _tool(
            "get_wazuh_agent_processes",
            "wazuh",
            ("current_process_state",),
            requires=("agent_id",),
            temporal_scope="current_only",
            priority=30,
            result_list_fields=("processes",),
        ),
        _tool(
            "get_wazuh_agent_ports",
            "wazuh",
            ("current_port_state",),
            requires=("agent_id", "protocol", "state"),
            temporal_scope="current_only",
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
) -> dict[str, Any]:
    """Ask for the long-window policy when the window needs it.

    The planner writes the window; the row limits and window caps are the MCP's,
    and it does not have them. A month-long question was reaching the Zabbix MCP
    under the default policy and coming back capped at 26 hours, which the
    report then read as the whole month.
    """
    updated = dict(arguments)
    argument = policy.window_policy_argument
    if not argument or _present(updated.get(argument)):
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
