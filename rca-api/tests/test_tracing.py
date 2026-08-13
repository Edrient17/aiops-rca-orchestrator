"""Tracing is configuration, and the off state has to be the safe one.

The LangSmith SDK reads the environment, so a stale LANGSMITH_TRACING left in a
container would turn tracing on without a key and make every model call retry
against an endpoint that rejects it. Being off has to mean written off, not
merely unset.
"""

import os

import pytest
from pydantic import SecretStr

from aiops_rca.config.settings import Settings
from aiops_rca.services.tracing import configure, wrap_openai_client


def make_settings(**overrides) -> Settings:
    base = {
        "aiops_internal_token": SecretStr("t"),
        "openai_api_key": SecretStr("sk-test"),
        "zabbix_mcp_url": "http://zabbix/mcp",
        "zabbix_mcp_auth_token": SecretStr("z"),
        "oss_es_mcp_url": "http://es/mcp",
        "wazuh_mcp_url": "http://wazuh/mcp",
        "wazuh_mcp_auth_token": SecretStr("w"),
    }
    return Settings(**{**base, **overrides})


@pytest.fixture(autouse=True)
def clean_environment():
    saved = {k: os.environ.get(k) for k in list(os.environ) if k.startswith("LANGSMITH_")}
    for key in saved:
        os.environ.pop(key, None)
    yield
    for key in list(os.environ):
        if key.startswith("LANGSMITH_"):
            os.environ.pop(key, None)
    os.environ.update({k: v for k, v in saved.items() if v is not None})


def test_tracing_stays_off_without_a_key():
    assert configure(make_settings(langsmith_tracing=True)) is False
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_tracing_stays_off_when_a_key_is_present_but_unrequested():
    settings = make_settings(langsmith_api_key=SecretStr("ls-key"))
    assert configure(settings) is False
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_a_stale_environment_flag_is_overwritten_rather_than_inherited():
    os.environ["LANGSMITH_TRACING"] = "true"
    assert configure(make_settings()) is False
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_enabling_points_the_sdk_at_the_configured_project():
    settings = make_settings(
        langsmith_tracing=True,
        langsmith_api_key=SecretStr("ls-key"),
        langsmith_project="aiops-rca-staging",
    )
    assert configure(settings) is True
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "ls-key"
    assert os.environ["LANGSMITH_PROJECT"] == "aiops-rca-staging"
    assert os.environ["LANGSMITH_ENDPOINT"] == "https://api.smith.langchain.com"


def test_the_client_is_returned_untouched_when_tracing_is_off():
    # Wrapping unconditionally would put the tracing SDK in the path of every
    # deployment, including ones that never send a trace anywhere.
    sentinel = object()
    assert wrap_openai_client(sentinel, enabled=False) is sentinel
