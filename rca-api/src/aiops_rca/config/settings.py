"""Environment-backed settings with fail-fast production validation."""

from typing import Annotated

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: Annotated[int, Field(ge=1, le=65535)] = 8090
    aiops_internal_token: SecretStr

    openai_api_key: SecretStr
    openai_base_url: str | None = None
    # Which model each stage gets. The five investigation nodes shared one
    # setting, so the step that reads a host name out of a JSON response was
    # billed at whatever the step that weighs evidence against hypotheses
    # needed -- and both moved together, so neither could be tuned.
    #
    # The split is by what the step decides, not by where it sits in the graph.
    # A step that judges -- what was observed, what could explain it, what the
    # evidence does to those explanations, what to look at next -- aims the
    # whole investigation, and a wrong judgement produces a confident report
    # about the wrong thing. A step that fetches a name or fills in a schema
    # fails loudly and gets another turn.
    rca_reasoning_model: str = "gpt-5.6-terra"
    rca_routine_model: str = "gpt-5.6-luna"
    rca_question_model: str = "gpt-5.6-luna"
    rca_writer_model: str = "gpt-5.6-terra"

    zabbix_mcp_url: str
    zabbix_mcp_auth_token: SecretStr
    oss_es_mcp_url: str
    wazuh_mcp_url: str
    wazuh_mcp_auth_token: SecretStr

    mcp_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 120
    model_timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 180
    mcp_retry_attempts: Annotated[int, Field(ge=1, le=3)] = 2

    # Tracing is off unless a key is present, so a deployment without LangSmith
    # behaves exactly as before rather than failing or silently retrying.
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "aiops-rca"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    @property
    def tracing_enabled(self) -> bool:
        return bool(
            self.langsmith_tracing
            and self.langsmith_api_key
            and self.langsmith_api_key.get_secret_value()
        )

    @model_validator(mode="after")
    def reject_empty_secrets(self) -> "Settings":
        required = {
            "AIOPS_INTERNAL_TOKEN": self.aiops_internal_token,
            "OPENAI_API_KEY": self.openai_api_key,
            "ZABBIX_MCP_AUTH_TOKEN": self.zabbix_mcp_auth_token,
            "WAZUH_MCP_AUTH_TOKEN": self.wazuh_mcp_auth_token,
        }
        empty = [
            name for name, value in required.items() if not value.get_secret_value()
        ]
        if empty:
            raise ValueError(f"required secrets are empty: {', '.join(empty)}")
        return self
