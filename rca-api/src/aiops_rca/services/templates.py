"""Deterministic report-template selection and investigation window calculation."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiops_rca.api.models import ReportTemplate
from aiops_rca.schemas.investigation import (
    InvestigationLimits,
    RequestEnvelope,
    UnknownItem,
)
from aiops_rca.schemas.parsed_request import (
    AbsoluteWindowHint,
    ParsedRequest,
    RelativeWindowHint,
)
from aiops_rca.services.template_contract import declared_effects, parse_sections

# Used when the analyzer names no range at all.
DEFAULT_WINDOW_UNSPECIFIED = timedelta(hours=3)


def select_template(
    requested_id: str,
    templates: list[ReportTemplate],
) -> tuple[ReportTemplate, UnknownItem | None]:
    """Pick the requested kind, or fall back and say that it happened.

    The fallback is deliberate -- report kinds are rows an operator adds, so an
    id this build has never heard of should still produce an investigation. But
    a silent fallback makes a misclassification indistinguishable from a
    correct one, and the first LangGraph run to hit it invented a kind, landed
    on incident_rca by luck, and reported nothing unusual.
    """
    enabled = [item for item in templates if item.enabled]
    selected = next(
        (item for item in enabled if item.template_id == requested_id),
        None,
    )
    if selected:
        return selected, None
    fallback = next(
        (item for item in enabled if item.template_id == "incident_rca"),
        None,
    )
    if fallback is None:
        raise ValueError("incident_rca fallback template is missing")
    return fallback, UnknownItem(
        code="report_kind_not_in_catalog",
        message=(
            f"요청된 보고서 종류 '{requested_id}'가 카탈로그에 없어 "
            f"'{fallback.template_id}'로 조사했다. 조사 범위와 구성이 "
            f"요청과 다를 수 있다."
        ),
    )


def prepare_collection(
    template: ReportTemplate,
    parsed: ParsedRequest,
    request: RequestEnvelope,
) -> tuple[dict[str, object], InvestigationLimits]:
    collection = dict(template.collection)
    collection["resolved_window"] = resolve_window(collection, parsed, request)
    # The two halves of a template used to be independent: `output` said what
    # to write and `collection` said what to gather, with nothing checking that
    # the second could supply the first. Carrying the sections' declaration
    # into the collection block is what lets the sweep and the stop guard see
    # it.
    collection["required_effects"] = list(
        declared_effects(parse_sections(template.output)),
    )
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

    hint = parsed.initial_window_hint
    if isinstance(hint, AbsoluteWindowHint):
        return _window(hint.from_.astimezone(UTC), hint.to.astimezone(UTC))

    anchor = (parsed.anchor_time or received).astimezone(UTC)
    if isinstance(hint, RelativeWindowHint):
        return _window(
            anchor - timedelta(minutes=hint.before_minutes),
            anchor + timedelta(minutes=hint.after_minutes),
        )

    # No hint at all. The former default was thirty minutes either side, which
    # is the narrowest window this system can open: an analyzer that omits the
    # field blinds the investigation at the first step, and nothing downstream
    # can tell that from a question that genuinely concerned one hour. Three
    # hours is still bounded, and DEFAULT_WINDOW_UNSPECIFIED is recorded so the
    # collector can say the range was assumed rather than asked for.
    return _window(
        anchor - DEFAULT_WINDOW_UNSPECIFIED,
        anchor + DEFAULT_WINDOW_UNSPECIFIED,
    )


def _window(start: datetime, end: datetime) -> dict[str, str]:
    """Window boundaries at second precision.

    isoformat() prints microseconds when the datetime carries them, and one
    does whenever no anchor was named in the question: the fallback is the
    request's arrival time, straight from the ingress clock. Six fractional
    digits then reached an MCP that accepts three, and a question about the
    present -- which never names a time -- failed at the first event scan.

    Seconds are also what the query means. Zabbix stores event times as epoch
    seconds, so the extra digits could not have selected anything.
    """
    return {
        "from": _instant(start),
        "to": _instant(end),
    }


def _instant(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")
