"""Every evidence source this service can reach, described once.

Adding an MCP server used to mean finding six files that each knew part of the
answer: the tool source literal, the adapter set, the transport table, the
generic-evidence prefix, the evidence id pattern, and the settings fields.
Missing one of them failed in a different way each time, and the worst of them
-- the generic-evidence prefix -- failed with a KeyError in the middle of a
live investigation.

They all read from here now. A new source is one entry in SOURCES plus its two
settings fields; the pieces that used to be scattered are derived.
"""

from dataclasses import dataclass
from typing import Literal

# Static rather than derived from SOURCES because a Literal has to be readable
# by the type checker. The test suite holds the two in agreement.
ToolSource = Literal["zabbix", "elasticsearch", "wazuh"]


@dataclass(frozen=True)
class SourceProfile:
    """What the rest of the service needs to know about one MCP server."""

    name: ToolSource
    #: Settings attribute holding the server's /mcp URL.
    url_setting: str
    #: Settings attribute holding its bearer token, or None when it takes none.
    token_setting: str | None
    #: Evidence id prefix and evidence type for output with no dedicated
    #: normalizer. Every source needs one: without it a tool whose result is
    #: not specially shaped raises at normalization time.
    generic_prefix: str
    generic_evidence_type: str
    #: Every evidence id prefix this source may produce, including the ones its
    #: dedicated normalizers emit. The evidence id pattern is built from these,
    #: so a new prefix is legal as soon as it is named here.
    evidence_prefixes: tuple[str, ...]


SOURCES: dict[str, SourceProfile] = {
    "zabbix": SourceProfile(
        name="zabbix",
        url_setting="zabbix_mcp_url",
        token_setting="zabbix_mcp_auth_token",
        generic_prefix="zbx:object",
        generic_evidence_type="observation",
        evidence_prefixes=("zbx:event", "zbx:trigger", "zbx:metric", "zbx:object"),
    ),
    "elasticsearch": SourceProfile(
        name="elasticsearch",
        url_setting="oss_es_mcp_url",
        token_setting=None,
        generic_prefix="log:lines",
        generic_evidence_type="log_lines",
        evidence_prefixes=("log:summary", "log:lines"),
    ),
    "wazuh": SourceProfile(
        name="wazuh",
        url_setting="wazuh_mcp_url",
        token_setting="wazuh_mcp_auth_token",
        generic_prefix="wazuh:alerts",
        generic_evidence_type="audit_alerts",
        evidence_prefixes=("wazuh:alerts",),
    ),
}


def evidence_id_pattern() -> str:
    """Regex accepting exactly the evidence ids the known sources produce."""

    prefixes = sorted(
        {prefix for profile in SOURCES.values() for prefix in profile.evidence_prefixes},
    )
    return "^(" + "|".join(prefixes) + "):.+$"
