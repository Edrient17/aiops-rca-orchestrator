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
    # One model per stage. A stage left unset follows `rca_model`, so a
    # deployment with no opinion sets one variable and a deployment that wants
    # to move a single node can do that without touching any other.
    #
    # Only the writer ships above the default, because it produces the thing an
    # operator actually reads and there is no later step to catch a bad one.
    #
    # The stages worth raising next are the ones that judge: what was observed,
    # what could explain it, what the evidence does to those explanations, and
    # what to look at next. A wrong judgement there aims the whole
    # investigation and comes back as a confident report about the wrong thing.
    # They are on the default until that is measured rather than assumed.
    rca_model: str = "gpt-5.6-luna"
    rca_model_question_analyzer: str | None = None
    rca_model_resolve_hosts: str | None = None
    rca_model_establish_phenomenon: str | None = None
    rca_model_hypothesis_planner: str | None = None
    rca_model_observation_planner: str | None = None
    rca_model_hypothesis_updater: str | None = None
    rca_model_report_writer: str | None = "gpt-5.6-terra"

    def model_for(self, stage: str) -> str:
        """The model this stage runs on, or the default it falls back to.

        A stage name with no setting behind it raises rather than quietly
        returning the default: the graph is built at startup, so a typo here
        stops the service instead of silently downgrading one node.
        """
        return getattr(self, f"rca_model_{stage}") or self.rca_model

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
