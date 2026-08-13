"""Stable request and response envelope between n8n and the RCA service."""

from typing import Annotated, Any, Literal

from pydantic import Field

from aiops_rca.schemas.base import StrictModel, TemplateId
from aiops_rca.schemas.evidence_package import EvidencePackage
from aiops_rca.schemas.investigation import RequestEnvelope
from aiops_rca.schemas.parsed_request import ParsedRequest
from aiops_rca.schemas.report import Report


class ReportTemplate(StrictModel):
    template_id: TemplateId
    version: Annotated[int, Field(ge=1)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(min_length=1, max_length=1000)]
    enabled: bool = True
    collection: dict[str, Any]
    output: dict[str, Any]


class InvestigationApiRequest(StrictModel):
    request: RequestEnvelope
    prior_question: Annotated[str, Field(max_length=5000)] | None = None
    templates: Annotated[list[ReportTemplate], Field(min_length=1, max_length=100)]


class AgentRun(StrictModel):
    stage: Literal["question_analyzer", "evidence_collector", "rca_writer"]
    status: Literal["succeeded"] = "succeeded"
    model: Annotated[str, Field(min_length=1, max_length=200)]
    duration_ms: Annotated[int, Field(ge=0)]
    output: Any


class InvestigationTrace(StrictModel):
    visited_nodes: list[str]
    tool_calls: list[dict[str, Any]]
    stop_reason: str | None


class InvestigationApiResponse(StrictModel):
    status: Literal["completed", "needs_clarification", "unsupported"]
    investigation_id: Annotated[str, Field(min_length=1, max_length=200)]
    parsed_request: ParsedRequest
    template: ReportTemplate
    evidence_package: EvidencePackage | None
    report: Report | None
    agent_runs: list[AgentRun]
    trace: InvestigationTrace | None
