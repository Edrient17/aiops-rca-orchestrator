"""End-to-end orchestration used by the synchronous HTTP endpoint."""

import asyncio
from datetime import UTC, datetime
from importlib.resources import files
from time import perf_counter
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver

from aiops_rca.api.models import (
    AgentRun,
    InvestigationApiRequest,
    InvestigationApiResponse,
    InvestigationTrace,
)
from aiops_rca.config.settings import Settings
from aiops_rca.graph.builder import CollectorNodes, build_collector_graph
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
from aiops_rca.schemas.report import Report
from aiops_rca.services.llm import OpenAIStructuredModel, StructuredModel
from aiops_rca.services.templates import prepare_collection, select_template
from aiops_rca.tools.adapters.base import AdapterSet, McpAdapter
from aiops_rca.tools.adapters.streamable_http import StreamableHttpMcpTransport
from aiops_rca.tools.executor import ToolExecutor
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY, ToolRegistry


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
        parsed = await self.model.complete(
            model=self.settings.rca_question_model,
            output_type=ParsedRequest,
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

        template = select_template(parsed.request_type, api_request.templates)
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
            unknowns=catalog_unknowns,
        )
        started = perf_counter()
        output = await self.graph.ainvoke(
            state,
            config={
                "configurable": {"thread_id": investigation_id},
                "recursion_limit": 8 + limits.max_iterations * 6,
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
        report = await self.model.complete(
            model=self.settings.rca_writer_model,
            output_type=Report,
            system_prompt=_prompt("rca_writer.md"),
            payload={
                "parsed_request": parsed.model_dump(mode="json"),
                "evidence_package": package.model_dump(mode="json", by_alias=True),
                "report_guidance": template.output.get("guidance", ""),
                "sections": _writer_sections(template.output, package),
            },
            reasoning_effort="medium",
        )
        _validate_report(report, template.output, package)
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
        sources = ("zabbix", "elasticsearch", "wazuh")
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
                except ValueError:
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
                        "input_schema": _without_examples(
                            item.get("inputSchema") or {}
                        ),
                        "output_schema": _without_examples(
                            item.get("outputSchema") or {}
                        ),
                    }
                )
        return catalog, unknowns


def build_live_service(settings: Settings) -> InvestigationService:
    model = OpenAIStructuredModel(
        api_key=settings.openai_api_key.get_secret_value(),
        base_url=settings.openai_base_url,
        timeout_seconds=settings.model_timeout_seconds,
    )
    transports = {
        "zabbix": StreamableHttpMcpTransport(
            settings.zabbix_mcp_url,
            bearer_token=settings.zabbix_mcp_auth_token.get_secret_value(),
            timeout_seconds=settings.mcp_timeout_seconds,
            retry_attempts=settings.mcp_retry_attempts,
        ),
        "elasticsearch": StreamableHttpMcpTransport(
            settings.oss_es_mcp_url,
            timeout_seconds=settings.mcp_timeout_seconds,
            retry_attempts=settings.mcp_retry_attempts,
        ),
        "wazuh": StreamableHttpMcpTransport(
            settings.wazuh_mcp_url,
            bearer_token=settings.wazuh_mcp_auth_token.get_secret_value(),
            timeout_seconds=settings.mcp_timeout_seconds,
            retry_attempts=settings.mcp_retry_attempts,
        ),
    }
    adapters = AdapterSet(
        **{
            source: McpAdapter(
                source=source,
                registry=DEFAULT_TOOL_REGISTRY,
                transport=transport,
                timeout_seconds=settings.mcp_timeout_seconds,
            )
            for source, transport in transports.items()
        }
    )
    return InvestigationService(settings=settings, model=model, adapters=adapters)


def _writer_sections(output: dict[str, Any], package: Any) -> list[dict[str, Any]]:
    has_problem_event = any(
        item.evidence_id.startswith("zbx:event:")
        and item.resource_ids.event_id is not None
        for item in package.evidence
    )
    return [
        {
            key: section[key]
            for key in ("id", "heading", "instruction", "required")
            if key in section
        }
        for section in output.get("sections", [])
        if not section.get("requires_problem_event") or has_problem_event
    ]


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
