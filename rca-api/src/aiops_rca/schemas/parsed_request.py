"""Pydantic equivalent of ``schemas/parsed-request.schema.json``."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from aiops_rca.schemas.base import StrictModel, TemplateId


class InitialWindowHint(StrictModel):
    before_minutes: Annotated[int, Field(ge=5, le=1440)]
    after_minutes: Annotated[int, Field(ge=5, le=1440)]


class ParsedRequest(StrictModel):
    schema_version: Literal["0.1.0"] = "0.1.0"
    request_id: Annotated[str, Field(min_length=1, max_length=200)]
    parse_status: Literal["ready", "needs_clarification", "unsupported"]
    request_type: TemplateId
    host_queries: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=255)]],
        Field(max_length=20),
    ]
    anchor_time: AwareDatetime | None
    timezone: Annotated[str, Field(min_length=1, max_length=100)]
    incident_description: Annotated[str, Field(min_length=1, max_length=2000)]
    incident_type_hint: Annotated[str, Field(max_length=200)] | None
    user_intent: Annotated[str, Field(min_length=1, max_length=500)]
    initial_window_hint: InitialWindowHint | None
    allow_dynamic_expansion: bool
    ambiguities: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=500)]],
        Field(max_length=20),
    ]
    original_question: Annotated[str, Field(min_length=1, max_length=5000)]
