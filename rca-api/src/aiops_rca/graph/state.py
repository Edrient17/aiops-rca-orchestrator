"""Source-of-truth state checkpointed between diagnostic graph nodes."""

from datetime import datetime
from typing import Annotated, Any

from pydantic import AwareDatetime, Field, model_validator

from aiops_rca.schemas.base import StrictModel
from aiops_rca.schemas.evidence_package import Evidence, EvidencePackage
from aiops_rca.schemas.investigation import (
    Hypothesis,
    InvestigationLimits,
    KnownFact,
    ObservationQuestion,
    PlannedToolCall,
    RequestEnvelope,
    ResolvedHost,
    UnknownItem,
)
from aiops_rca.schemas.parsed_request import ParsedRequest
from aiops_rca.schemas.report import Report
from aiops_rca.tools.result import ToolExecutionResult


class InvestigationState(StrictModel):
    investigation_id: Annotated[str, Field(min_length=1, max_length=200)]
    request: RequestEnvelope
    parsed_request: ParsedRequest
    collection: dict[str, Any] | None = None

    hosts: Annotated[list[ResolvedHost], Field(max_length=20)] = Field(
        default_factory=list
    )
    unresolved_hosts: Annotated[list[str], Field(max_length=20)] = Field(
        default_factory=list
    )

    phenomenon: Annotated[str, Field(max_length=2000)] | None = None
    hypotheses: Annotated[list[Hypothesis], Field(max_length=20)] = Field(
        default_factory=list
    )
    known_facts: Annotated[list[KnownFact], Field(max_length=100)] = Field(
        default_factory=list
    )
    unknowns: Annotated[list[UnknownItem], Field(max_length=100)] = Field(
        default_factory=list
    )

    next_question: ObservationQuestion | None = None
    planned_tool_call: PlannedToolCall | None = None
    candidate_tool_arguments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: Tool name to the host its call is about, by name. The id it may not
    #: have is looked up from `hosts` where a tool needs it.
    candidate_tool_hosts: dict[str, str] = Field(default_factory=dict)
    generic_fallback_allowed: bool = False
    # Discovered once per investigation and checkpointed so every planning
    # turn sees one consistent set of live MCP contracts.
    tool_catalog: list[dict[str, Any]] = Field(default_factory=list)

    evidence: Annotated[list[Evidence], Field(max_length=200)] = Field(
        default_factory=list
    )
    last_observation: ToolExecutionResult | None = None
    tool_results: Annotated[list[ToolExecutionResult], Field(max_length=100)] = Field(
        default_factory=list,
    )
    tool_errors: Annotated[list[ToolExecutionResult], Field(max_length=100)] = Field(
        default_factory=list,
    )
    tool_call_purposes: dict[str, Annotated[str, Field(max_length=1000)]] = Field(
        default_factory=dict,
    )

    iteration_count: Annotated[int, Field(ge=0, le=20)] = 0
    tool_call_count: Annotated[int, Field(ge=0, le=100)] = 0
    limits: InvestigationLimits = Field(default_factory=InvestigationLimits)
    started_at: AwareDatetime
    stop_reason: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    limit_reached: bool = False
    fatal_error: Annotated[str, Field(min_length=1, max_length=10_000)] | None = None

    evidence_package: EvidencePackage | None = None

    #: The selected report template's output spec -- section ids, headings,
    #: which are required. Carried in state because the writer runs inside the
    #: graph now and the template was chosen before it started.
    template_output: dict[str, Any] | None = None
    report: Report | None = None
    #: What the checks said about the last draft, in the checker's own words.
    #: Handed back to the writer, which is the only way it learns that its own
    #: count or citation was rejected.
    report_findings: Annotated[list[str], Field(max_length=50)] = Field(
        default_factory=list,
    )
    #: Every finding that ever sent a draft back, kept across drafts.
    #: report_findings describes only the draft in hand and is cleared when it
    #: is replaced, so a rewrite that succeeded left no record of what was wrong
    #: -- and a check that is too eager would cost a second model call on every
    #: report with nothing naming it.
    report_rejections: Annotated[list[str], Field(max_length=100)] = Field(
        default_factory=list,
    )
    report_attempts: Annotated[int, Field(ge=0, le=10)] = 0
    #: Summed across attempts, so the audit row reports the whole cost of
    #: writing rather than the last pass.
    report_duration_ms: Annotated[int, Field(ge=0)] = 0

    visited_nodes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph_invariants(self) -> "InvestigationState":
        # Keyed on the name because that is what the sources share. A host
        # found in a log search has no Zabbix id, and two hosts with no id
        # are not the same host.
        _unique([host.host for host in self.hosts], "host")
        hypothesis_ids = [hypothesis.id for hypothesis in self.hypotheses]
        _unique(hypothesis_ids, "hypothesis id")
        _unique([item.evidence_id for item in self.evidence], "evidence_id")
        _unique([item.tool_call_id for item in self.tool_results], "tool_call_id")

        known_hypotheses = set(hypothesis_ids)
        known_evidence = {item.evidence_id for item in self.evidence}
        referenced_evidence = {
            evidence_id
            for fact in self.known_facts
            for evidence_id in fact.evidence_ids
        }
        for hypothesis in self.hypotheses:
            referenced_evidence.update(hypothesis.supporting_evidence_ids)
            referenced_evidence.update(hypothesis.counter_evidence_ids)
        if missing_evidence := referenced_evidence - known_evidence:
            raise ValueError(
                f"known_facts reference unknown evidence: {sorted(missing_evidence)}"
            )
        if self.next_question:
            missing = (
                set(self.next_question.discriminates_hypothesis_ids) - known_hypotheses
            )
            if missing:
                raise ValueError(
                    f"next_question references unknown hypotheses: {sorted(missing)}"
                )
        if self.planned_tool_call:
            missing = (
                set(self.planned_tool_call.target_hypothesis_ids) - known_hypotheses
            )
            if missing:
                raise ValueError(
                    f"planned_tool_call references unknown hypotheses: {sorted(missing)}"
                )
            if (
                self.planned_tool_call.host
                and self.planned_tool_call.host
                not in {host.host for host in self.hosts}
            ):
                raise ValueError("planned_tool_call references an unresolved host")
        if self.tool_call_count != len(self.tool_results):
            raise ValueError("tool_call_count must equal the number of tool_results")
        return self

    def elapsed_seconds(self, now: datetime) -> float:
        return max(0, (now - self.started_at).total_seconds())


def _unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} values must be unique")
