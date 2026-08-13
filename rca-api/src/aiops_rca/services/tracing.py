"""LangSmith tracing, enabled by configuration rather than by import.

LangGraph traces its own topology once the environment says to, so node
boundaries, state transitions and the loop show up without changes here. Model
calls do not: they go through the OpenAI SDK directly rather than a LangChain
runnable, so without wrapping the client a trace shows a node that took four
seconds and nothing about what was asked or answered -- which is most of what
there is to look at.
"""

import os

from openai import AsyncOpenAI

from aiops_rca.config.settings import Settings


def configure(settings: Settings) -> bool:
    """Point the LangSmith SDK at a project. Returns whether tracing is on.

    The SDK reads environment variables, so this sets them from settings rather
    than requiring the container to carry two spellings of the same values.
    """
    if not settings.tracing_enabled:
        # Explicitly off rather than absent: a stale LANGSMITH_TRACING in the
        # environment would otherwise turn tracing on without a key and make
        # every model call retry against an endpoint that rejects it.
        os.environ["LANGSMITH_TRACING"] = "false"
        return False

    assert settings.langsmith_api_key is not None
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key.get_secret_value()
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    return True


def wrap_openai_client(client: AsyncOpenAI, *, enabled: bool) -> AsyncOpenAI:
    """Make model calls appear in the trace with their prompts and outputs."""
    if not enabled:
        return client
    from langsmith.wrappers import wrap_openai

    return wrap_openai(client)
