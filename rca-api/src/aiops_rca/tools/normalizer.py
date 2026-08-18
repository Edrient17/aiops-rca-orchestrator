"""Deterministically convert heterogeneous MCP observations to Evidence."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from aiops_rca.schemas.evidence_package import Evidence
from aiops_rca.schemas.investigation import PlannedToolCall
from aiops_rca.tools.result import ToolExecutionResult


def normalize_observation(
    result: ToolExecutionResult,
    planned: PlannedToolCall,
    *,
    host_id: str,
    host: str,
) -> list[Evidence]:
    """Normalize only observed fields; errors produce unknowns, never evidence."""

    if result.status == "error":
        return []
    response = result.response
    if not isinstance(response, Mapping):
        return [_generic_evidence(result, planned, host_id=host_id, host=host)]

    if result.tool_name in {"get_incident_events", "get_related_events"}:
        return _event_evidence(result, response, host_id)
    if result.tool_name == "get_trigger_details":
        return [_trigger_evidence(result, response, host_id)]
    if result.tool_name == "get_metric_summary":
        return _metric_summary_evidence(result, response, host_id)
    if result.tool_name == "get_metric_history":
        return [_metric_history_evidence(result, response, host_id)]
    return [_generic_evidence(result, planned, host_id=host_id, host=host)]


def merge_evidence(
    existing: list[Evidence], additions: list[Evidence]
) -> list[Evidence]:
    """Deduplicate byte-identical IDs and reject conflicting reuse of an ID."""

    merged = {item.evidence_id: item for item in existing}
    for item in additions:
        prior = merged.get(item.evidence_id)
        if prior and prior != item:
            raise ValueError(f"conflicting evidence reuses id {item.evidence_id}")
        merged[item.evidence_id] = item
    return list(merged.values())


def _event_evidence(
    result: ToolExecutionResult,
    response: Mapping[str, Any],
    host_id: str,
) -> list[Evidence]:
    events = response.get("events")
    window = _window(response)
    if not isinstance(events, list) or not events:
        return [
            Evidence.model_validate(
                {
                    "evidence_id": f"zbx:event:none:{_fingerprint(result.request)}",
                    "evidence_type": "observation",
                    "source": "zabbix",
                    "summary": "No Zabbix problem event was returned for the requested host and window.",
                    "observed_at": None,
                    "window": window,
                    "resource_ids": _resource_ids(host_id),
                    "metric": None,
                    "data_quality": None,
                    "tool_call_id": result.tool_call_id,
                    "search_query": None,
                },
            ),
        ]

    output: list[Evidence] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_id = _digits(event.get("event_id"))
        trigger_id = _digits(event.get("trigger_id"))
        evidence_id = str(event.get("evidence_id") or f"zbx:event:{event_id}")
        name = str(event.get("name") or "Zabbix problem event")
        severity = event.get("severity")
        recovery = event.get("recovered_at")
        details = [name]
        if severity:
            details.append(f"severity={severity}")
        if recovery:
            details.append(f"recovered_at={recovery}")
        output.append(
            Evidence.model_validate(
                {
                    "evidence_id": evidence_id,
                    "evidence_type": "event",
                    "source": "zabbix",
                    "summary": "; ".join(details)[:3000],
                    "observed_at": event.get("started_at"),
                    "window": window,
                    "resource_ids": _resource_ids(
                        host_id,
                        event_id=event_id,
                        trigger_id=trigger_id,
                    ),
                    "metric": None,
                    "data_quality": None,
                    "tool_call_id": result.tool_call_id,
                    "search_query": None,
                },
            ),
        )
    return output


def _trigger_evidence(
    result: ToolExecutionResult,
    response: Mapping[str, Any],
    host_id: str,
) -> Evidence:
    trigger_id = _digits(response.get("trigger_id"))
    description = str(response.get("description") or "Zabbix trigger definition")
    expression = response.get("expression")
    summary = (
        description if not expression else f"{description}; expression={expression}"
    )
    return Evidence.model_validate(
        {
            "evidence_id": str(
                response.get("evidence_id") or f"zbx:trigger:{trigger_id}"
            ),
            "evidence_type": "trigger",
            "source": "zabbix",
            "summary": summary[:3000],
            "observed_at": None,
            "window": None,
            "resource_ids": _resource_ids(host_id, trigger_id=trigger_id),
            "metric": None,
            "data_quality": None,
            "tool_call_id": result.tool_call_id,
            "search_query": None,
        },
    )


def _metric_summary_evidence(
    result: ToolExecutionResult,
    response: Mapping[str, Any],
    host_id: str,
) -> list[Evidence]:
    series = response.get("series")
    if not isinstance(series, list):
        return []
    output: list[Evidence] = []
    for entry in series:
        if not isinstance(entry, Mapping):
            continue
        item = entry.get("item") if isinstance(entry.get("item"), Mapping) else {}
        summary = (
            entry.get("summary") if isinstance(entry.get("summary"), Mapping) else {}
        )
        item_id = _digits(item.get("item_id"))
        metric = _metric(item, summary)
        output.append(
            Evidence.model_validate(
                {
                    "evidence_id": entry.get("evidence_id")
                    or f"zbx:metric:{item_id}:{_fingerprint(result.request)}",
                    "evidence_type": "metric_summary",
                    "source": "zabbix",
                    "summary": _metric_text(metric),
                    "observed_at": None,
                    "window": _window(response),
                    "resource_ids": _resource_ids(host_id, item_id=item_id),
                    "metric": metric,
                    "data_quality": entry.get("data_quality"),
                    "tool_call_id": result.tool_call_id,
                    "search_query": None,
                },
            ),
        )
    return output


def _metric_history_evidence(
    result: ToolExecutionResult,
    response: Mapping[str, Any],
    host_id: str,
) -> Evidence:
    item = response.get("item") if isinstance(response.get("item"), Mapping) else {}
    summary = (
        response.get("summary") if isinstance(response.get("summary"), Mapping) else {}
    )
    item_id = _digits(item.get("item_id"))
    metric = _metric(item, summary)
    return Evidence.model_validate(
        {
            "evidence_id": response.get("evidence_id")
            or f"zbx:metric:{item_id}:{_fingerprint(result.request)}",
            "evidence_type": "metric_history",
            "source": "zabbix",
            "summary": _metric_text(metric),
            "observed_at": None,
            "window": _window(response),
            "resource_ids": _resource_ids(host_id, item_id=item_id),
            "metric": metric,
            "data_quality": response.get("data_quality"),
            "tool_call_id": result.tool_call_id,
            "search_query": None,
        },
    )


_GENERIC_SHAPE: dict[str, tuple[str, str]] = {
    "wazuh": ("wazuh:alerts", "audit_alerts"),
    "elasticsearch": ("log:lines", "log_lines"),
    "zabbix": ("zbx:object", "observation"),
}


def _generic_evidence(
    result: ToolExecutionResult,
    planned: PlannedToolCall,
    *,
    host_id: str,
    host: str,
) -> Evidence:
    # Branching on two sources filed every Zabbix tool without a dedicated
    # normalizer -- list_relevant_metrics, find_hosts, query_zabbix -- as an
    # Elasticsearch log line, and the report then cited a `log:lines` id as the
    # basis for a statement about Zabbix items. The result already carries the
    # source the registry assigned it, so there is nothing here to infer.
    prefix, evidence_type = _GENERIC_SHAPE[result.source]
    source = result.source
    response = result.response if isinstance(result.response, Mapping) else {}
    return Evidence.model_validate(
        {
            "evidence_id": f"{prefix}:{host}:{_fingerprint([planned.arguments, result.response])}",
            "evidence_type": evidence_type,
            "source": source,
            "summary": f"{planned.purpose}: {_json(result.response)}"[:3000],
            "observed_at": None,
            "window": _window_from_arguments(result.request),
            "resource_ids": _resource_ids(host_id),
            "metric": None,
            "data_quality": response.get("data_quality"),
            "tool_call_id": result.tool_call_id,
            "search_query": _search_query(planned.arguments),
        },
    )


def _search_query(arguments: Mapping[str, Any]) -> str | None:
    query = arguments.get("query")
    if isinstance(query, str):
        return query
    query_body = arguments.get("query_body")
    return _json(query_body) if query_body is not None else None


def _metric(item: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": str(item.get("name") or "unnamed metric"),
        "unit": item.get("unit"),
        "min": summary.get("min"),
        "max": summary.get("max"),
        "avg": summary.get("avg"),
        "first": summary.get("first"),
        "last": summary.get("last"),
        "change_percent": summary.get("change_percent"),
        "trend": summary.get("trend") or "insufficient_data",
        "key": item.get("key"),
    }


def _metric_text(metric: Mapping[str, Any]) -> str:
    return (
        f"{metric['name']}: min={metric['min']}, max={metric['max']}, "
        f"avg={metric['avg']}, first={metric['first']}, last={metric['last']}, "
        f"change_percent={metric['change_percent']}, trend={metric['trend']}"
    )[:3000]


def _window(response: Mapping[str, Any]) -> dict[str, Any] | None:
    window = response.get("window")
    if (
        not isinstance(window, Mapping)
        or not window.get("from")
        or not window.get("to")
    ):
        return None
    return {
        "from": window["from"],
        "to": window["to"],
        "aggregation": response.get("aggregation") or window.get("aggregation"),
    }


def _window_from_arguments(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    start = arguments.get("time_from")
    end = arguments.get("time_to")
    if not start or not end:
        return None
    return {"from": start, "to": end, "aggregation": arguments.get("aggregation")}


def _resource_ids(
    host_id: str,
    *,
    event_id: str | None = None,
    trigger_id: str | None = None,
    item_id: str | None = None,
) -> dict[str, str | None]:
    if not host_id.isdigit():
        raise ValueError("host_id must be a decimal string")
    return {
        "host_id": host_id,
        "event_id": event_id,
        "trigger_id": trigger_id,
        "item_id": item_id,
    }


def _digits(value: Any) -> str | None:
    text = str(value) if value is not None else ""
    return text if text.isdigit() else None


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
