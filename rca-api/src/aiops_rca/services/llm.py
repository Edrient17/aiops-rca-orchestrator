"""Small structured-output boundary around the OpenAI Responses API."""

import json
from typing import Protocol, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class StructuredModel(Protocol):
    async def complete(
        self,
        *,
        model: str,
        output_type: type[T],
        system_prompt: str,
        payload: object,
        reasoning_effort: str,
    ) -> T:
        """Return one schema-validated model result."""


class OpenAIStructuredModel:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout_seconds: float = 180,
        traced: bool = False,
    ) -> None:
        from aiops_rca.services.tracing import wrap_openai_client

        self.client = wrap_openai_client(
            AsyncOpenAI(api_key=api_key, base_url=base_url),
            enabled=traced,
        )
        self.timeout_seconds = timeout_seconds

    async def complete(
        self,
        *,
        model: str,
        output_type: type[T],
        system_prompt: str,
        payload: object,
        reasoning_effort: str,
    ) -> T:
        response = await self.client.responses.parse(
            model=model,
            instructions=system_prompt,
            input=json.dumps(payload, ensure_ascii=False, default=str),
            text_format=output_type,
            reasoning={"effort": reasoning_effort},
            store=False,
            timeout=self.timeout_seconds,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("model returned no structured output")
        return parsed
