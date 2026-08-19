"""Normalized result returned by every MCP adapter."""

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from aiops_rca.schemas.base import StrictModel
from aiops_rca.sources import ToolSource

ToolExecutionStatus = Literal["ok", "empty", "filtered_empty", "partial", "error"]


class ToolExecutionResult(StrictModel):
    tool_call_id: Annotated[str, Field(min_length=1, max_length=200)]
    tool_name: Annotated[str, Field(min_length=1, max_length=100)]
    source: ToolSource
    status: ToolExecutionStatus
    request: dict[str, Any]
    response: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    error: Annotated[str, Field(max_length=10_000)] | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime

    @model_validator(mode="after")
    def validate_result(self) -> "ToolExecutionResult":
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.status == "error" and not self.error:
            raise ValueError("error status requires an error message")
        if self.status != "error" and self.error:
            raise ValueError("only error results may carry an error message")
        return self
