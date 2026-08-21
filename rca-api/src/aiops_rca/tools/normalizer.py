"""Deterministically convert heterogeneous MCP observations to Evidence."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from aiops_rca.schemas.evidence_package import Evidence
from aiops_rca.schemas.investigation import PlannedToolCall, UnknownItem
from aiops_rca.sources import SOURCES
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY, ToolPolicyError
from aiops_rca.tools.result import ToolExecutionResult


def normalize_observation(
    result: ToolExecutionResult,
    planned: PlannedToolCall,
    *,
    host_id: str | None,
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
    host_id: str | None,
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
    host_id: str | None,
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
    host_id: str | None,
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
    host_id: str | None,
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
    host_id: str | None,
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
    response = result.response
    observed = _observed_list(result, response)
    return Evidence.model_validate(
        {
            "evidence_id": f"{prefix}:{host}:{_fingerprint([planned.arguments, result.response])}",
            "evidence_type": evidence_type,
            "source": source,
            # When the rows travel in `observed`, the summary says what is there
            # instead of carrying it. Both competing for the same 3000
            # characters is what cut a list of sixty services down to fifteen.
            "summary": _bounded(
                f"{planned.purpose}: {_observed_text(observed, response)}"
                if observed
                else f"{planned.purpose}: {_json(result.response)}"
            ),
            "observed_at": None,
            "window": _window_from_arguments(result.request),
            "resource_ids": _resource_ids(host_id),
            "metric": None,
            "observed": observed,
            # Only an object carries one. A reply that is prose with rows on
            # the end says its limits in the prose, which the summary keeps.
            "data_quality": _rounded_quality(
                response.get("data_quality")
                if isinstance(response, Mapping)
                else None
            ),
            "tool_call_id": result.tool_call_id,
            "search_query": _search_query(planned.arguments),
        },
    )


# How much of a list one piece of evidence carries. Four times the prose cap,
# which holds a real host's services with room over; a machine with hundreds of
# processes is trimmed and says by how many.
LIST_CAPACITY_CHARS = 12_000


def _trailing_rows(text: str) -> list[Any] | None:
    """The JSON array a reply ends with, if it ends with one.

    `esql` answers `Results\n[{...}, {...}]` and `list_indices` answers
    `Found 12 indices:\n[...]`: a sentence, then the data. Nothing in the
    reply is a Mapping, so the rows had nowhere to go but the summary, and a
    summary caps at three thousand characters -- which is where a
    seven-service, twenty-four-hour breakdown was lost, leaving a report
    section that said the distribution could not be determined when it had
    in fact been fetched.
    """
    start = text.find("[")
    if start < 0:
        return None
    try:
        parsed = json.loads(text[start:])
    except ValueError:
        return None
    return parsed if isinstance(parsed, list) and parsed else None


def _rows_in(
    result: ToolExecutionResult,
    response: Any,
) -> tuple[str, list[Any]] | None:
    """Where this reply keeps its rows, in whichever shape it returned them.

    An object names the field, and which field is the registry's
    `result_list_fields` -- the same declaration the result classifier reads,
    so a tool needs no per-tool knowledge here. A bare array is its own rows. A
    string is read for the array at its end, because the shape "prose then
    data" belongs to the reply rather than to any one tool.
    """
    if isinstance(response, Mapping):
        try:
            policy = DEFAULT_TOOL_REGISTRY.get(result.tool_name)
        except ToolPolicyError:
            return None
        for field in policy.result_list_fields:
            rows = response.get(field)
            if isinstance(rows, list) and rows:
                return field, rows
        return None
    if isinstance(response, list) and response:
        return "rows", response
    if isinstance(response, str):
        rows = _trailing_rows(response)
        if rows:
            return "rows", rows
    return None


def _observed_list(
    result: ToolExecutionResult,
    response: Any,
) -> dict[str, Any] | None:
    """The rows this observation returned, if it returned rows."""
    found = _rows_in(result, response)
    if found is None:
        return None
    kind, rows = found
    items: list[dict[str, Any]] = []
    budget = LIST_CAPACITY_CHARS
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        cost = len(_json(row))
        if cost > budget and items:
            break
        budget -= cost
        items.append(dict(row))
    if not items:
        return None
    return {
        "kind": kind,
        "items": items,
        "omitted": len(rows) - len(items),
    }


def _observed_text(observed: Mapping[str, Any], response: Any) -> str:
    """A sentence about the list, not the list."""

    kind = observed["kind"]
    carried = len(observed["items"])
    omitted = observed["omitted"]
    parts = [f"{carried} {kind}"]
    if omitted:
        parts.append(f"({omitted} more not carried here)")
    # Anything the tool said besides the rows -- counts, limits, its own
    # partial flag -- still belongs in the sentence.
    if isinstance(response, Mapping):
        rest = {key: value for key, value in response.items() if key != kind}
        if rest:
            parts.append(_json(rest))
    elif isinstance(response, str):
        # The prose the rows were stuck on the end of. It is where a reply of
        # this shape puts its total, and the rows are a page of it.
        said = response[: response.find("[")].strip() if "[" in response else ""
        if said:
            parts.append(said)
    return " ".join(parts)


#: What `Evidence.search_query` will accept. Held here as well so a query too
#: long to store is dropped rather than raised: this field exists so a footnote
#: can reopen the citation, and a query cut in half cannot be re-run, so a
#: partial one is worth less than none. Losing it costs a footnote; raising it
#: costs the whole investigation, which is what happened once.
MAX_SEARCH_QUERY_CHARS = 4000


def _search_query(arguments: Mapping[str, Any]) -> str | None:
    query = arguments.get("query")
    if not isinstance(query, str):
        query_body = arguments.get("query_body")
        query = _json(query_body) if query_body is not None else None
    if query is None or len(query) > MAX_SEARCH_QUERY_CHARS:
        return None
    return query


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
    host_id: str | None,
    *,
    event_id: str | None = None,
    trigger_id: str | None = None,
    item_id: str | None = None,
) -> dict[str, str | None]:
    if host_id is not None and not host_id.isdigit():
        # None is a host Zabbix does not know -- found in a log search or an
        # agent list. A non-numeric string is a programming error.
        raise ValueError("host_id must be a decimal string or None")
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
