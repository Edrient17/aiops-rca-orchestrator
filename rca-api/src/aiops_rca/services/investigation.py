"""End-to-end orchestration used by the synchronous HTTP endpoint."""

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import lru_cache
from importlib.resources import files
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import create_model

from aiops_rca.api.models import (
    AgentRun,
    InvestigationApiRequest,
    InvestigationApiResponse,
    InvestigationTrace,
)
from aiops_rca.config.settings import Settings
from aiops_rca.graph.builder import CollectorNodes, build_collector_graph
from aiops_rca.graph.coverage_nodes import CoverageSweepNode
from aiops_rca.graph.deterministic_nodes import (
    EvidenceNormalizerNode,
    ResolveHostsNode,
    StopGuardNode,
    ToolExecutorNode,
    ToolRouterNode,
)
from aiops_rca.graph.live_nodes import (
    EstablishPhenomenonNode,
    EvidencePackageBuilderNode,
    HypothesisPlannerNode,
    HypothesisUpdaterNode,
    ObservationPlannerNode,
)
from aiops_rca.graph.state import InvestigationState
from aiops_rca.schemas.investigation import UnknownItem
from aiops_rca.schemas.parsed_request import ParsedRequest
from aiops_rca.schemas.report import Report, ReportItem, ReportSection
from aiops_rca.services.llm import OpenAIStructuredModel, StructuredModel
from aiops_rca.services.template_contract import parse_sections
from aiops_rca.services.templates import prepare_collection, select_template
from aiops_rca.services.tracing import configure as configure_tracing
from aiops_rca.sources import SOURCES
from aiops_rca.tools.adapters.base import AdapterSet, McpAdapter
from aiops_rca.tools.adapters.streamable_http import StreamableHttpMcpTransport
from aiops_rca.tools.executor import ToolExecutor
from aiops_rca.tools.registry import (
    DEFAULT_TOOL_REGISTRY,
    ToolPolicyError,
    ToolRegistry,
)


class InvestigationService:
    def __init__(
        self,
        *,
        settings: Settings,
        model: StructuredModel,
        adapters: AdapterSet,
        registry: ToolRegistry = DEFAULT_TOOL_REGISTRY,
    ) -> None:
        self.settings = settings
        self.model = model
        self.registry = registry
        self.adapters = adapters
        executor = ToolExecutor(adapters, registry)
        self.graph = build_collector_graph(
            CollectorNodes(
                resolve_hosts=ResolveHostsNode(adapters.zabbix),
                establish_phenomenon=EstablishPhenomenonNode(
                    zabbix=adapters.zabbix,
                    model=model,
                    model_name=settings.rca_investigation_model,
                ),
                coverage_sweep=CoverageSweepNode(
                    executor=executor,
                    registry=registry,
                ),
                hypothesis_planner=HypothesisPlannerNode(
                    model=model,
                    model_name=settings.rca_investigation_model,
                ),
                observation_planner=ObservationPlannerNode(
                    model=model,
                    model_name=settings.rca_investigation_model,
                    registry=registry,
                ),
                tool_router=ToolRouterNode(registry),
                tool_executor=ToolExecutorNode(executor),
                evidence_normalizer=EvidenceNormalizerNode(),
                hypothesis_updater=HypothesisUpdaterNode(
                    model=model,
                    model_name=settings.rca_investigation_model,
                ),
                stop_guard=StopGuardNode(),
                evidence_package_builder=EvidencePackageBuilderNode(),
            ),
            checkpointer=InMemorySaver(),
        )

    async def investigate(
        self,
        api_request: InvestigationApiRequest,
    ) -> InvestigationApiResponse:
        investigation_id = f"inv-{uuid4()}"
        runs: list[AgentRun] = []

        started = perf_counter()
        catalog_ids = [
            item.template_id for item in api_request.templates if item.enabled
        ]
        parsed = await self.model.complete(
            model=self.settings.rca_question_model,
            output_type=_analyzer_output_type(tuple(catalog_ids)),
            system_prompt=_prompt("question_analyzer.md"),
            payload={
                "request_id": api_request.request.request_id,
                "question": api_request.request.question,
                "received_at": api_request.request.received_at,
                "default_timezone": api_request.request.timezone,
                "prior_question": api_request.prior_question,
                "answers_clarification": bool(api_request.prior_question),
                "report_catalog": [
                    {
                        "id": item.template_id,
                        "title": item.title,
                        "when_to_use": item.description,
                        "supplies_hosts": item.collection.get("host_selector", {}).get(
                            "mode"
                        )
                        != "from_question",
                        "supplies_window": item.collection.get("window", {}).get(
                            "range"
                        )
                        != "anchor_relative",
                    }
                    for item in api_request.templates
                    if item.enabled
                ],
            },
            reasoning_effort="low",
        )
        if parsed.request_id != api_request.request.request_id:
            raise ValueError("question analyzer changed request_id")
        runs.append(
            AgentRun(
                stage="question_analyzer",
                model=self.settings.rca_question_model,
                duration_ms=_elapsed_ms(started),
                output=parsed.model_dump(mode="json"),
            )
        )

        template, template_unknown = select_template(
            parsed.request_type, api_request.templates
        )
        if parsed.parse_status != "ready":
            return InvestigationApiResponse(
                status=parsed.parse_status,
                investigation_id=investigation_id,
                parsed_request=parsed,
                template=template,
                evidence_package=None,
                report=None,
                agent_runs=runs,
                trace=None,
            )

        collection, limits = prepare_collection(template, parsed, api_request.request)
        tool_catalog, catalog_unknowns = await self._load_tool_catalog()
        state = InvestigationState(
            investigation_id=investigation_id,
            request=api_request.request,
            parsed_request=parsed,
            collection=collection,
            limits=limits,
            # The Slack/webhook timestamp can predate execution by minutes when a
            # request is retried. Runtime budgets must start when this process
            # actually begins collecting evidence.
            started_at=datetime.now(UTC),
            tool_catalog=tool_catalog,
            unknowns=(
                [*catalog_unknowns, template_unknown]
                if template_unknown
                else catalog_unknowns
            ),
        )
        started = perf_counter()
        output = await self.graph.ainvoke(
            state,
            config={
                "configurable": {"thread_id": investigation_id},
                "recursion_limit": 8 + limits.max_iterations * 6,
                # A trace is only useful if the run can be found again from a
                # Slack thread or a failed report, so it carries the ids that
                # appear everywhere else.
                "run_name": f"investigation {api_request.request.request_id}",
                "metadata": {
                    "request_id": api_request.request.request_id,
                    "investigation_id": investigation_id,
                    "host_queries": parsed.host_queries,
                    "model": self.settings.rca_investigation_model,
                },
                "tags": ["evidence_collector", parsed.request_type],
            },
        )
        finished = InvestigationState.model_validate(output)
        package = finished.evidence_package
        runs.append(
            AgentRun(
                stage="evidence_collector",
                model=self.settings.rca_investigation_model,
                duration_ms=_elapsed_ms(started),
                output=(
                    package.model_dump(mode="json", by_alias=True)
                    if package
                    else {"stop_reason": finished.stop_reason}
                ),
            )
        )
        trace = InvestigationTrace(
            visited_nodes=finished.visited_nodes,
            tool_calls=[item.model_dump(mode="json") for item in finished.tool_results],
            stop_reason=finished.stop_reason,
        )
        if package is None:
            ambiguities = [item.message for item in finished.unknowns]
            parsed = parsed.model_copy(
                update={
                    "parse_status": "needs_clarification",
                    "ambiguities": ambiguities
                    or ["조사할 호스트를 하나 이상 확인할 수 없습니다."],
                }
            )
            return InvestigationApiResponse(
                status="needs_clarification",
                investigation_id=investigation_id,
                parsed_request=parsed,
                template=template,
                evidence_package=None,
                report=None,
                agent_runs=runs,
                trace=trace,
            )

        started = perf_counter()
        report = await write_report(
            self.model,
            self.settings.rca_writer_model,
            parsed=parsed,
            package=package,
            template_output=template.output,
            uncovered_effects=output.get("uncovered_effects") or (),
        )
        runs.append(
            AgentRun(
                stage="rca_writer",
                model=self.settings.rca_writer_model,
                duration_ms=_elapsed_ms(started),
                output=report.model_dump(mode="json"),
            )
        )
        return InvestigationApiResponse(
            status="completed",
            investigation_id=investigation_id,
            parsed_request=parsed,
            template=template,
            evidence_package=package,
            report=report,
            agent_runs=runs,
            trace=trace,
        )

    async def _load_tool_catalog(
        self,
    ) -> tuple[list[dict[str, Any]], list[UnknownItem]]:
        sources = tuple(SOURCES)
        results = await asyncio.gather(
            *(self.adapters.for_source(source).list_tools() for source in sources),
            return_exceptions=True,
        )
        catalog: list[dict[str, Any]] = []
        unknowns: list[UnknownItem] = []
        for source, result in zip(sources, results, strict=True):
            if isinstance(result, BaseException):
                unknowns.append(
                    UnknownItem(
                        code="tool_catalog_unavailable",
                        message=(
                            f"{source} MCP tool catalog is unavailable: "
                            f"{str(result)[:1000]}"
                        ),
                    )
                )
                continue
            for item in result:
                name = item.get("name")
                if not isinstance(name, str):
                    continue
                try:
                    policy = self.registry.get(name)
                except ToolPolicyError:
                    continue
                if policy.source != source or policy.blocked_reason:
                    continue
                catalog.append(
                    {
                        "name": name,
                        "source": source,
                        "kind": policy.kind,
                        "effects": policy.effects,
                        "temporal_scope": policy.temporal_scope,
                        "description": str(item.get("description") or "")[:4000],
                        # No output_schema: not one of these servers declares
                        # one, so it was an empty object sent on every turn of
                        # the loop under a name that promised a contract.
                        "input_schema": _without_examples(
                            item.get("inputSchema") or {}
                        ),
                    }
                )
        return catalog, unknowns


def build_live_service(settings: Settings) -> InvestigationService:
    traced = configure_tracing(settings)
    model = OpenAIStructuredModel(
        api_key=settings.openai_api_key.get_secret_value(),
        base_url=settings.openai_base_url,
        timeout_seconds=settings.model_timeout_seconds,
        traced=traced,
    )
    # Built from the source table so adding an MCP server does not mean
    # remembering this function exists.
    adapters = AdapterSet(
        {
            profile.name: McpAdapter(
                source=profile.name,
                registry=DEFAULT_TOOL_REGISTRY,
                transport=StreamableHttpMcpTransport(
                    getattr(settings, profile.url_setting),
                    bearer_token=(
                        getattr(settings, profile.token_setting).get_secret_value()
                        if profile.token_setting
                        else None
                    ),
                    timeout_seconds=settings.mcp_timeout_seconds,
                    retry_attempts=settings.mcp_retry_attempts,
                ),
                timeout_seconds=settings.mcp_timeout_seconds,
            )
            for profile in SOURCES.values()
        },
    )
    return InvestigationService(settings=settings, model=model, adapters=adapters)


async def write_report(
    model: Any,
    model_name: str,
    *,
    parsed: ParsedRequest,
    package: Any,
    template_output: dict[str, Any],
    uncovered_effects: Iterable[str] = (),
) -> Report:
    """Turn a finished evidence package into a report.

    Lifted out of `investigate` so it can be run on its own against a package
    that is already on disk. The writer is where a report says twenty-five of a
    list of twenty-six, and re-running the whole investigation to exercise it
    means live infrastructure, three MCP servers, and an answer that has moved
    since yesterday. Given a stored package, this is the same call the service
    makes, with nothing to reach.
    """
    report = await model.complete(
        model=model_name,
        output_type=Report,
        system_prompt=_prompt("rca_writer.md"),
        payload={
            "parsed_request": parsed.model_dump(mode="json"),
            "evidence_package": package.model_dump(mode="json", by_alias=True),
            "report_guidance": template_output.get("guidance", ""),
            "sections": _writer_sections(template_output, package, uncovered_effects),
        },
        reasoning_effort="medium",
    )
    _validate_report(report, template_output, package)
    return _reconcile_evidence(report, template_output, package)


def _writer_sections(
    output: dict[str, Any],
    package: Any,
    uncovered_effects: Iterable[str] = (),
) -> list[dict[str, Any]]:
    has_problem_event = any(
        item.evidence_id.startswith("zbx:event:")
        and item.resource_ids.event_id is not None
        for item in package.evidence
    )
    uncovered = set(uncovered_effects)
    sections: list[dict[str, Any]] = []
    for section in parse_sections(output):
        if section.requires_problem_event and not has_problem_event:
            continue
        payload: dict[str, Any] = {"id": section.id, "required": section.required}
        if section.heading:
            payload["heading"] = section.heading
        if section.instruction:
            payload["instruction"] = section.instruction
        missing = sorted(set(section.requires_effects) & uncovered)
        if missing:
            # Without this the writer sees a section it cannot fill and no
            # reason why, and the report gets an unexplained empty heading.
            payload["evidence_unavailable"] = missing
        sections.append(payload)
    return sections


def _reconcile_evidence(
    report: Report,
    output: dict[str, Any],
    package: Any,
) -> Report:
    """Make sure a discrete event cannot leave the investigation unmentioned.

    A `/etc/passwd has been changed` event was collected, reached the
    phenomenon summary, and appeared in no section of the finished report: the
    availability section counted only the outages and no other section owned
    it. Metrics are summarized in aggregate and are not expected to be cited
    one by one, but an event is a thing that happened, and dropping one is a
    silent loss.
    """
    cited = {
        ref
        for section in report.sections
        for item in section.items
        for ref in [*item.evidence_refs, *item.counter_evidence_refs]
    }
    dropped = [
        item
        for item in package.evidence
        if item.evidence_type == "event" and item.evidence_id not in cited
    ]
    if not dropped:
        return report
    target = next(
        (
            item.get("id")
            for item in output.get("sections", [])
            if item.get("id") == "limitations"
        ),
        None,
    )
    if target is None:
        return report
    items = [
        ReportItem(
            text=f"보고서 본문에 실리지 않은 관측 이벤트: {item.summary}"[:2000],
            label="미반영 이벤트",
            evidence_refs=[item.evidence_id],
            counter_evidence_refs=[],
        )
        for item in dropped[:20]
    ]
    sections = list(report.sections)
    for index, section in enumerate(sections):
        if section.id == target:
            sections[index] = section.model_copy(
                update={"items": [*section.items, *items]},
            )
            break
    else:
        sections.append(ReportSection(id=target, body=None, items=items))
    return report.model_copy(update={"sections": sections})


def _validate_report(report: Report, output: dict[str, Any], package: Any) -> None:
    allowed_sections = {item.get("id") for item in output.get("sections", [])}
    section_ids = [item.id for item in report.sections]
    if len(section_ids) != len(set(section_ids)):
        raise ValueError("report section ids must be unique")
    if set(section_ids) - allowed_sections:
        raise ValueError("report contains a section not declared by the template")
    evidence_ids = {item.evidence_id for item in package.evidence}
    references = {
        ref
        for section in report.sections
        for item in section.items
        for ref in [*item.evidence_refs, *item.counter_evidence_refs]
    }
    if references - evidence_ids:
        raise ValueError("report references evidence outside the package")


def _prompt(name: str) -> str:
    return files("aiops_rca.prompts").joinpath(name).read_text(encoding="utf-8")


@lru_cache(maxsize=32)
def _analyzer_output_type(catalog_ids: tuple[str, ...]) -> type[ParsedRequest]:
    """ParsedRequest with request_type narrowed to the catalog of this request.

    The stored contract deliberately keeps request_type a free string: report
    kinds are rows an operator adds, so a fixed enum in the schema could never
    grow, and an id matching nothing falls back to incident_rca.

    That tolerance belongs at the boundary where the value is read, not where it
    is produced. Asked for a kind with a two-row catalog in front of it, the
    analyzer answered "incident_inquiry" -- a plausible name for the question,
    and not one of the two. The fallback then chose correctly and silently, so
    a wrong classification looked exactly like a right one. Structured output
    with a Literal makes the invented id unrepresentable rather than survivable.
    """
    if not catalog_ids:
        return ParsedRequest
    return create_model(
        "CatalogBoundParsedRequest",
        __base__=ParsedRequest,
        request_type=(Literal[catalog_ids], ...),  # type: ignore[valid-type]
    )


def _without_examples(value: Any) -> Any:
    """Keep live constraints while preventing example values from steering plans."""

    if isinstance(value, dict):
        return {
            key: _without_examples(item)
            for key, item in value.items()
            if key not in {"example", "examples"}
        }
    if isinstance(value, list):
        return [_without_examples(item) for item in value]
    return value


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))
