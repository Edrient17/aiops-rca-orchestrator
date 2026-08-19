"""Pydantic equivalent of ``schemas/evidence-package.schema.json``."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from aiops_rca.schemas.base import StrictModel, ZabbixId
from aiops_rca.sources import ToolSource, evidence_id_pattern

EvidenceRef = Annotated[str, Field(min_length=1, max_length=300)]
RequiredEvidenceRefs = Annotated[
    list[EvidenceRef],
    Field(min_length=1, max_length=50),
]
OptionalEvidenceRefs = Annotated[list[EvidenceRef], Field(max_length=50)]


class EvidenceWindow(StrictModel):
    from_: AwareDatetime = Field(alias="from", serialization_alias="from")
    to: AwareDatetime
    aggregation: Literal["raw", "1m", "5m", "15m", "1h", "6h", "1d"] | None = None

    @model_validator(mode="after")
    def require_ordered_window(self) -> "EvidenceWindow":
        if self.to <= self.from_:
            raise ValueError("window.to must be later than window.from")
        return self


class EvidenceRequest(StrictModel):
    request_id: Annotated[str, Field(min_length=1, max_length=200)]
    original_question: Annotated[str, Field(min_length=1, max_length=5000)]
    requested_by: Annotated[str, Field(min_length=1, max_length=200)]


class QueryHost(StrictModel):
    host: Annotated[str, Field(min_length=1, max_length=255)]
    host_id: ZabbixId


class QueryContext(StrictModel):
    hosts: Annotated[list[QueryHost], Field(min_length=1, max_length=20)]
    timezone: Annotated[str, Field(min_length=1, max_length=100)]
    anchor_time: AwareDatetime


class ToolCallRecord(StrictModel):
    tool_call_id: Annotated[str, Field(min_length=1, max_length=200)]
    tool_name: Annotated[str, Field(min_length=1, max_length=100)]
    purpose: Annotated[str, Field(min_length=1, max_length=1000)]
    status: Literal["success", "partial", "failed"]


class InvestigationRecord(StrictModel):
    initial_window: EvidenceWindow
    final_window: EvidenceWindow
    iterations: Annotated[int, Field(ge=1, le=20)]
    tool_calls: Annotated[list[ToolCallRecord], Field(max_length=100)]
    expansion_reasons: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=1000)]],
        Field(max_length=20),
    ]
    stop_reason: Annotated[str, Field(min_length=1, max_length=2000)]
    limit_reached: bool


class ResourceIds(StrictModel):
    host_id: ZabbixId
    event_id: ZabbixId | None
    trigger_id: ZabbixId | None
    item_id: ZabbixId | None


class MetricSummary(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=500)]
    unit: Annotated[str, Field(max_length=100)] | None
    min: float | None
    max: float | None
    avg: float | None
    first: float | None
    last: float | None
    change_percent: float | None
    trend: Literal["increasing", "decreasing", "stable", "insufficient_data"]
    key: Annotated[str, Field(max_length=500)] | None = None


class MetricDataQuality(StrictModel):
    data_source: Literal["history", "trends"]
    sample_count: Annotated[int, Field(ge=0)]
    coverage_ratio: Annotated[float, Field(ge=0, le=1)] | None
    partial: bool
    returned_points: Annotated[int, Field(ge=0)] | None = None
    expected_buckets: Annotated[int, Field(ge=0)] | None = None


class LogFormats(StrictModel):
    spring: Annotated[int, Field(ge=0)] | None = None
    timestamped: Annotated[int, Field(ge=0)] | None = None
    syslog: Annotated[int, Field(ge=0)] | None = None
    unrecognised: Annotated[int, Field(ge=0)] | None = None


class EmptyBecauseFiltered(StrictModel):
    lines_in_window: Annotated[int, Field(ge=0)]
    matched_by_filters: Annotated[int, Field(ge=0)]


class LogDataQuality(StrictModel):
    data_source: Literal["logs"]
    partial: bool
    sampled_fraction: Annotated[float, Field(ge=0, le=1)] | None = None
    unlevelled_lines: Annotated[int, Field(ge=0)] | None = None
    formats: LogFormats | None = None
    scanned: Annotated[int, Field(ge=0)] | None = None
    matched_after_level_filter: Annotated[int, Field(ge=0)] | None = None
    messages_truncated: Annotated[int, Field(ge=0)] | None = None
    empty_because_filtered: EmptyBecauseFiltered | None = None
    omitted_from_middle: Annotated[int, Field(ge=0)] | None = None


DataQuality = Annotated[
    MetricDataQuality | LogDataQuality,
    Field(discriminator="data_source"),
]


class Evidence(StrictModel):
    evidence_id: Annotated[
        str,
        Field(
            pattern=evidence_id_pattern()
        ),
    ]
    evidence_type: Literal[
        "event",
        "trigger",
        "metric_summary",
        "metric_history",
        "log_summary",
        "log_lines",
        "audit_alerts",
        "observation",
    ]
    source: ToolSource
    summary: Annotated[str, Field(min_length=1, max_length=3000)]
    observed_at: AwareDatetime | None
    window: EvidenceWindow | None
    resource_ids: ResourceIds
    metric: MetricSummary | None
    data_quality: DataQuality | None
    tool_call_id: Annotated[str, Field(min_length=1, max_length=200)]
    search_query: Annotated[str, Field(max_length=1000)] | None = None


class ConfirmedFact(StrictModel):
    fact: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_refs: RequiredEvidenceRefs


class PackageHypothesis(StrictModel):
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    supporting_evidence_refs: OptionalEvidenceRefs
    contradicting_evidence_refs: OptionalEvidenceRefs
    confidence: Literal["high", "medium", "low"]


class EvidencePackage(StrictModel):
    schema_version: Literal["0.1.0"] = "0.1.0"
    request: EvidenceRequest
    query_context: QueryContext
    investigation: InvestigationRecord
    observed_failure_mode: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence: Annotated[list[Evidence], Field(max_length=200)]
    confirmed_facts: Annotated[list[ConfirmedFact], Field(max_length=100)]
    hypotheses: Annotated[list[PackageHypothesis], Field(max_length=20)]
    unknowns: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=2000)]],
        Field(max_length=100),
    ]

    @model_validator(mode="after")
    def require_unique_and_valid_references(self) -> "EvidencePackage":
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence_id values must be unique")

        known = set(evidence_ids)
        referenced = {
            ref for fact in self.confirmed_facts for ref in fact.evidence_refs
        }
        for hypothesis in self.hypotheses:
            referenced.update(hypothesis.supporting_evidence_refs)
            referenced.update(hypothesis.contradicting_evidence_refs)
        missing = referenced - known
        if missing:
            raise ValueError(f"evidence references do not exist: {sorted(missing)}")
        return self
