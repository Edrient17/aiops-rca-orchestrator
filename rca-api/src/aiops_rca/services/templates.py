"""Deterministic report-template selection and investigation window calculation."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiops_rca.api.models import ReportTemplate
from aiops_rca.schemas.investigation import InvestigationLimits, RequestEnvelope
from aiops_rca.schemas.parsed_request import ParsedRequest


def select_template(
    requested_id: str,
    templates: list[ReportTemplate],
) -> ReportTemplate:
    enabled = [item for item in templates if item.enabled]
    selected = next(
        (item for item in enabled if item.template_id == requested_id),
        None,
    )
    if selected:
        return selected
    fallback = next(
        (item for item in enabled if item.template_id == "incident_rca"),
        None,
    )
    if fallback is None:
        raise ValueError("incident_rca fallback template is missing")
    return fallback


def prepare_collection(
    template: ReportTemplate,
    parsed: ParsedRequest,
    request: RequestEnvelope,
) -> tuple[dict[str, object], InvestigationLimits]:
    collection = dict(template.collection)
    collection["resolved_window"] = resolve_window(collection, parsed, request)
    limits = collection.get("limits")
    limits = limits if isinstance(limits, dict) else {}
    host_count = max(1, len(parsed.host_queries))
    resolved_limits = InvestigationLimits(
        max_iterations=int(limits.get("max_iterations") or 10),
        max_tool_calls=min(
            100,
            int(limits.get("max_tool_calls") or min(60, 10 + 20 * host_count)),
        ),
        max_duration_seconds=int(limits.get("max_duration_seconds") or 600),
    )
    return collection, resolved_limits


def resolve_window(
    collection: dict[str, object],
    parsed: ParsedRequest,
    request: RequestEnvelope,
) -> dict[str, str]:
    window_policy = collection.get("window")
    window_policy = window_policy if isinstance(window_policy, dict) else {}
    range_name = str(window_policy.get("range") or "anchor_relative")
    received = request.received_at.astimezone(UTC)

    if range_name in {"last_7_days", "last_30_days"}:
        days = 7 if range_name == "last_7_days" else 30
        return _window(received - timedelta(days=days), received)
    if range_name == "last_calendar_month":
        local = received.astimezone(ZoneInfo(parsed.timezone))
        current_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_last = current_start - timedelta(days=1)
        previous_start = previous_last.replace(day=1)
        return _window(previous_start.astimezone(UTC), current_start.astimezone(UTC))
    if range_name != "anchor_relative":
        raise ValueError(f"unsupported collection window range: {range_name}")

    anchor = parsed.anchor_time or received
    hint = parsed.initial_window_hint
    before = hint.before_minutes if hint else 30
    after = hint.after_minutes if hint else 30
    return _window(
        anchor.astimezone(UTC) - timedelta(minutes=before),
        anchor.astimezone(UTC) + timedelta(minutes=after),
    )


def _window(start: datetime, end: datetime) -> dict[str, str]:
    return {
        "from": start.isoformat().replace("+00:00", "Z"),
        "to": end.isoformat().replace("+00:00", "Z"),
    }
