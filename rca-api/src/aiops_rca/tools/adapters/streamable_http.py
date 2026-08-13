"""Live MCP transport over the protocol's Streamable HTTP connection."""

import asyncio
import json
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent


class McpRemoteToolError(RuntimeError):
    """The MCP server handled the request and returned a tool-level error."""


class StreamableHttpMcpTransport:
    """Open a bounded MCP session per call and return decoded structured data."""

    def __init__(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        timeout_seconds: float = 120,
        retry_attempts: int = 2,
    ) -> None:
        self.url = url
        self.headers = (
            {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
        )
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = retry_attempts

    async def list_tools(self) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                return await self._list_once()
            except Exception as error:
                last_error = error
                if attempt + 1 < self.retry_attempts:
                    await asyncio.sleep(0.25 * (attempt + 1))
        assert last_error is not None
        raise last_error

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                return await self._call_once(tool_name, arguments)
            except McpRemoteToolError:
                raise
            except Exception as error:
                last_error = error
                if attempt + 1 < self.retry_attempts:
                    await asyncio.sleep(0.25 * (attempt + 1))
        assert last_error is not None
        raise last_error

    async def _call_once(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> Any:
        async with httpx.AsyncClient(
            headers=self.headers,
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=False,
        ) as client:
            async with streamable_http_client(self.url, http_client=client) as streams:
                read_stream, write_stream, _ = streams
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        tool_name,
                        arguments=dict(arguments),
                        read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
                    )

        if result.isError:
            raise McpRemoteToolError(_content_text(result.content) or "MCP tool failed")
        if result.structuredContent is not None:
            return result.structuredContent

        text = _content_text(result.content)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    async def _list_once(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(
            headers=self.headers,
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=False,
        ) as client:
            async with streamable_http_client(self.url, http_client=client) as streams:
                read_stream, write_stream, _ = streams
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools: list[dict[str, Any]] = []
                    cursor: str | None = None
                    while True:
                        page = await session.list_tools(cursor=cursor)
                        tools.extend(
                            tool.model_dump(
                                mode="json",
                                by_alias=True,
                                exclude_none=True,
                            )
                            for tool in page.tools
                        )
                        cursor = page.nextCursor
                        if not cursor:
                            return tools


def _content_text(content: list[Any]) -> str:
    return "\n".join(
        block.text for block in content if isinstance(block, TextContent)
    ).strip()
