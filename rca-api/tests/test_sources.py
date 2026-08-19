"""Adding an evidence source must not be able to go half-done.

The pieces a source needs used to live in six files. Whichever one you forgot
failed in its own way, and the worst failed with a KeyError in the middle of a
live investigation. They read from SOURCES now, and these tests hold SOURCES
and the things derived from it in agreement.
"""

import re
from typing import get_args

import pytest

from aiops_rca.config.settings import Settings
from aiops_rca.schemas.evidence_package import Evidence
from aiops_rca.sources import SOURCES, ToolSource, evidence_id_pattern
from aiops_rca.tools.registry import DEFAULT_TOOL_REGISTRY


def test_the_literal_and_the_table_name_the_same_sources():
    # The Literal has to be static for the type checker, so it cannot be
    # generated. This is what keeps the two from drifting.
    assert set(get_args(ToolSource)) == set(SOURCES)


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_a_profile_agrees_with_its_own_key(name):
    assert SOURCES[name].name == name


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_a_profile_names_settings_that_exist(name):
    # A typo here would surface as an AttributeError at service construction,
    # which is startup -- better, but still later than this.
    profile = SOURCES[name]
    fields = Settings.model_fields
    assert profile.url_setting in fields, profile.url_setting
    if profile.token_setting:
        assert profile.token_setting in fields, profile.token_setting


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_a_profile_declares_its_generic_prefix_among_its_prefixes(name):
    # The generic prefix is an evidence id like any other, so the id pattern
    # has to accept it or every generic observation fails validation.
    profile = SOURCES[name]
    assert profile.generic_prefix in profile.evidence_prefixes


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_every_declared_prefix_is_accepted_by_the_pattern(name):
    pattern = re.compile(evidence_id_pattern())
    for prefix in SOURCES[name].evidence_prefixes:
        assert pattern.match(f"{prefix}:something"), prefix


def test_an_unknown_prefix_is_still_refused():
    pattern = re.compile(evidence_id_pattern())
    assert not pattern.match("bogus:1")
    assert not pattern.match("zbx:unknown:1")


def test_a_prefix_is_not_claimed_by_two_sources():
    # Two sources sharing a prefix would make an evidence id ambiguous about
    # where it came from.
    seen: dict[str, str] = {}
    for name, profile in SOURCES.items():
        for prefix in profile.evidence_prefixes:
            assert prefix not in seen, f"{prefix} claimed by {seen.get(prefix)} and {name}"
            seen[prefix] = name


def test_every_registered_tool_belongs_to_a_known_source():
    for policy in DEFAULT_TOOL_REGISTRY.list():
        assert policy.source in SOURCES, policy.name


def test_the_generic_evidence_type_is_one_the_schema_allows():
    allowed = set(get_args(Evidence.model_fields["evidence_type"].annotation))
    for name, profile in SOURCES.items():
        assert profile.generic_evidence_type in allowed, name


def test_a_source_with_no_token_is_expressible():
    # The OSS Elasticsearch MCP takes no auth. Requiring a token setting would
    # have forced an empty secret into the settings contract.
    assert SOURCES["elasticsearch"].token_setting is None
