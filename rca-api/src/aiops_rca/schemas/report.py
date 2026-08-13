"""Pydantic equivalent of ``schemas/report.schema.json``."""

from typing import Annotated, Literal

from pydantic import Field

from aiops_rca.schemas.base import StrictModel, TemplateId

EvidenceRef = Annotated[str, Field(min_length=1, max_length=300)]


class ReportItem(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    label: Annotated[str, Field(max_length=40)] | None
    evidence_refs: Annotated[list[EvidenceRef], Field(max_length=50)]
    counter_evidence_refs: Annotated[list[EvidenceRef], Field(max_length=50)]


class ReportSection(StrictModel):
    id: TemplateId
    body: Annotated[str, Field(max_length=5000)] | None = None
    items: Annotated[list[ReportItem], Field(max_length=100)] = Field(
        default_factory=list,
    )


class Report(StrictModel):
    schema_version: Literal["0.1.0"] = "0.1.0"
    title: Annotated[str, Field(min_length=1, max_length=300)]
    sections: Annotated[list[ReportSection], Field(min_length=1, max_length=30)]
