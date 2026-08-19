"""Deterministically convert heterogeneous MCP observations to Evidence."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from aiops_rca.schemas.evidence_package import Evidence
from aiops_rca.schemas.investigation import PlannedToolCall, UnknownItem
from aiops_rca.sources import SOURCES
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


# What the graph state will hold. Exceeding it used to be a validation error
# raised after collection, which discarded a whole investigation over the last
# item added; the ceiling is applied here so it ends collection instead.
EVIDENCE_CAPACITY = 200


def merge_evidence(
    existing: list[Evidence],
    additions: list[Evidence],
    *,
    capacity: int = EVIDENCE_CAPACITY,
) -> tuple[list[Evidence], list[UnknownItem]]:
    """Merge by id, keeping the run alive whatever the additions look like.

    Two things used to end an investigation here, both after every tool call had
    already been paid for.

    An id is derived from the request, so the same query asked twice carries the
    same id -- and if the answer moved in between, the mismatch was raised as a
    programming error. It is not one: two readings of the same thing at
    different moments are two observations. The later one is kept, because it is
    the one still true, and the disagreement is recorded rather than thrown.

    The other was the ceiling. Collection now stops at it and says so, which
    leaves a report to write from what was gathered.
    """
    merged = {item.evidence_id: item for item in existing}
    unknowns: list[UnknownItem] = []
    dropped = 0
    for item in additions:
        prior = merged.get(item.evidence_id)
        if prior is not None and prior != item:
            unknowns.append(
                UnknownItem(
                    code="evidence_superseded",
                    message=(
                        f"{item.evidence_id} was observed twice with different"
                        " content; the later reading is the one kept"
                    ),
                    tool_call_id=item.tool_call_id,
                ),
            )
        elif prior is None and len(merged) >= capacity:
            dropped += 1
            continue
        merged[item.evidence_id] = item
    if dropped:
        unknowns.append(
            UnknownItem(
                code="evidence_capacity_reached",
                message=(
                    f"collection stopped at {capacity} pieces of evidence;"
                    f" {dropped} further observation(s) were not recorded"
                ),
            ),
        )
    return list(merged.values()), unknowns


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
                    "summary": _bounded("; ".join(details)),
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
            "summary": _bounded(summary),
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
                    "data_quality": _rounded_quality(entry.get("data_quality")),
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
            "data_quality": _rounded_quality(response.get("data_quality")),
            "tool_call_id": result.tool_call_id,
            "search_query": None,
        },
    )


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
    profile = SOURCES[result.source]
    prefix, evidence_type = profile.generic_prefix, profile.generic_evidence_type
    source = result.source
    response = result.response if isinstance(result.response, Mapping) else {}
    return Evidence.model_validate(
        {
            "evidence_id": f"{prefix}:{host}:{_fingerprint([planned.arguments, result.response])}",
            "evidence_type": evidence_type,
            "source": source,
            "summary": _bounded(f"{planned.purpose}: {_json(result.response)}"),
            "observed_at": None,
            "window": _window_from_arguments(result.request),
            "resource_ids": _resource_ids(host_id),
            "metric": None,
            "data_quality": _rounded_quality(response.get("data_quality")),
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


# Aggregate arithmetic produces every digit a float can hold, and the report
# printed them: a filesystem "월초 3.230541%에서 월말 3.300203%로", a buffer of
# "377053866.666667B". The trailing digits are not measurements -- they are the
# remainder of dividing by a sample count -- and they cost the reader the
# comparison the sentence exists to make.
DECIMAL_PLACES = 3
# Above this the fraction is noise rather than detail: a byte count or a
# packet counter has nothing meaningful after the point, and three decimals
# there lengthen the number without telling anyone anything.
WHOLE_NUMBER_ABOVE = 10_000


def _rounded(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if abs(value) >= WHOLE_NUMBER_ABOVE:
        return round(value)
    return round(value, DECIMAL_PLACES)


def _metric(item: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": str(item.get("name") or "unnamed metric"),
        "unit": item.get("unit"),
        "min": _rounded(summary.get("min")),
        "max": _rounded(summary.get("max")),
        "avg": _rounded(summary.get("avg")),
        "first": _rounded(summary.get("first")),
        "last": _rounded(summary.get("last")),
        "change_percent": _rounded(summary.get("change_percent")),
        "trend": summary.get("trend") or "insufficient_data",
        "key": item.get("key"),
    }


# The shapes Evidence.data_quality can be tagged as. A tool free to return any
# shape it likes -- query_zabbix reports row_limit and hit_row_limit, which
# describe a raw query and not a measurement -- would otherwise be handed to a
# discriminated union that cannot tag it, and the whole investigation fails at
# the point where its evidence is being written down.
_TAGGABLE_DATA_SOURCES = frozenset({"history", "trends", "logs"})


def _rounded_quality(quality: Any) -> Any:
    """The quality block, if the schema can tag it, with its ratio rounded.

    Anything else is dropped rather than passed on. The full response is
    already in the evidence summary, so nothing is lost that a reader could
    have used, and a shape the schema does not know is not worth failing an
    investigation over.
    """
    if not isinstance(quality, Mapping):
        return None
    if quality.get("data_source") not in _TAGGABLE_DATA_SOURCES:
        return None
    ratio = quality.get("coverage_ratio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return dict(quality)
    return {**quality, "coverage_ratio": round(ratio, DECIMAL_PLACES)}


def _metric_text(metric: Mapping[str, Any]) -> str:
    return _bounded(
        f"{metric['name']}: min={metric['min']}, max={metric['max']}, "
        f"avg={metric['avg']}, first={metric['first']}, last={metric['last']}, "
        f"change_percent={metric['change_percent']}, trend={metric['trend']}"
    )


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


# Evidence.summary is capped by the schema, and the cap used to be applied with
# a slice. Whatever sat past it disappeared with nothing to show for it: a
# process list arrived as its first three kilobytes, and the report described
# what it had been given as though that were the whole of it. The notice costs
# a few characters of the budget and buys the reader the one fact the slice
# destroyed.
SUMMARY_CAPACITY = 3000


def _bounded(text: str) -> str:
    if len(text) <= SUMMARY_CAPACITY:
        return text
    notice = f" …[요약 잘림: 전체 {len(text)}자]"
    return text[: SUMMARY_CAPACITY - len(notice)] + notice
