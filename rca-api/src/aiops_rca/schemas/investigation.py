"""Internal models for explicit diagnostic state and the n8n API envelope."""

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field

from aiops_rca.schemas.base import StrictModel, ZabbixId


class RequestEnvelope(StrictModel):
    request_id: Annotated[str, Field(min_length=1, max_length=200)]
    source: Annotated[str, Field(min_length=1, max_length=100)]
    received_at: AwareDatetime
    timezone: Annotated[str, Field(min_length=1, max_length=100)]
    question: Annotated[str, Field(min_length=1, max_length=5000)]
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResolvedHost(StrictModel):
    host: Annotated[str, Field(min_length=1, max_length=255)]
    host_id: ZabbixId
    query: Annotated[str, Field(min_length=1, max_length=255)] | None = None


class UnknownItem(StrictModel):
    code: Annotated[str, Field(min_length=1, max_length=100)]
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    host_query: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    tool_call_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class KnownFact(StrictModel):
    fact: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_ids: Annotated[list[str], Field(min_length=1, max_length=50)]


class Hypothesis(StrictModel):
    id: Annotated[str, Field(min_length=1, max_length=100)]
    statement: Annotated[str, Field(min_length=1, max_length=2000)]
    status: Literal["active", "supported", "rejected", "unresolved"] = "active"
    supporting_evidence_ids: Annotated[list[str], Field(max_length=50)] = Field(
        default_factory=list,
    )
    counter_evidence_ids: Annotated[list[str], Field(max_length=50)] = Field(
        default_factory=list,
    )
    rationale: Annotated[str, Field(max_length=2000)] | None = None


class ObservationQuestion(StrictModel):
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    discriminates_hypothesis_ids: Annotated[
        list[str], Field(min_length=1, max_length=20)
    ]
    expected_if_true: dict[str, str] = Field(default_factory=dict)
    expected_if_false: dict[str, str] = Field(default_factory=dict)
    temporal_scope: Literal["historical", "current", "timeless"] = "timeless"
    required_effect: Annotated[str, Field(min_length=1, max_length=100)] | None = None


class PlannedToolCall(StrictModel):
    tool_name: Annotated[str, Field(min_length=1, max_length=100)]
    arguments: dict[str, Any]
    purpose: Annotated[str, Field(min_length=1, max_length=1000)]
    target_hypothesis_ids: Annotated[list[str], Field(max_length=20)]
    # Association metadata for evidence normalization. It is deliberately not
    # inserted into arguments because MCP input schemas reject unknown fields.
    host_id: ZabbixId | None = None


class InvestigationLimits(StrictModel):
    max_tool_calls: Annotated[int, Field(ge=1, le=100)] = 30
    max_iterations: Annotated[int, Field(ge=1, le=20)] = 10
    max_duration_seconds: Annotated[int, Field(ge=1, le=3600)] = 300
