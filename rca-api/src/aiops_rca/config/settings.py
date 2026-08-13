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
    rca_question_model: str = "gpt-5.4-mini"
    rca_investigation_model: str = "gpt-5.6-terra"
    rca_writer_model: str = "gpt-5.4-mini"

    zabbix_mcp_url: str
    zabbix_mcp_auth_token: SecretStr
    oss_es_mcp_url: str
    wazuh_mcp_url: str
    wazuh_mcp_auth_token: SecretStr

    mcp_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 120
    model_timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 180
    mcp_retry_attempts: Annotated[int, Field(ge=1, le=3)] = 2

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
