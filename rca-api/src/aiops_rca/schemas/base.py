"""Shared strict-model configuration and constrained scalar types."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


class StrictModel(BaseModel):
    """Reject undeclared state rather than silently losing investigation data."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        validate_assignment=True,
    )


NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
ZabbixId = Annotated[str, StringConstraints(pattern=r"^\d+$")]
TemplateId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,63}$"),
]
