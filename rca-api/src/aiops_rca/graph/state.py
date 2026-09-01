"""Source-of-truth state checkpointed between diagnostic graph nodes."""

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any

from pydantic import AwareDatetime, Field, model_validator

from aiops_rca.schemas.base import StrictModel
from aiops_rca.schemas.evidence_package import Evidence, EvidencePackage
from aiops_rca.schemas.investigation import (
    Hypothesis,
    InvestigationLimits,
    KnownFact,
    ObservationQuestion,
    PlannedToolCall,
    RequestEnvelope,
    ResolvedHost,
    UnknownItem,
)
from aiops_rca.schemas.parsed_request import ParsedRequest
from aiops_rca.schemas.report import Report
from aiops_rca.tools.result import ToolExecutionResult

#: What this state will hold of the two lists that only ever grow. Thirty-odd
#: call sites append an unknown and none of them prunes, and the updater adds a
#: fact per turn, against ceilings this model enforces. Reaching one used to
#: raise out of the graph and discard the whole investigation over the last
#: item added -- after every tool call had been paid for.
#:
#: `merge_evidence` was changed for exactly this reason and stops collection at
#: its ceiling instead. This is the same bargain, applied where the state is
#: built rather than at each append, because a ceiling enforced in one place
#: cannot be forgotten by the next site that appends.
UNKNOWNS_CAPACITY = 100
KNOWN_FACTS_CAPACITY = 100

#: The unknown that says the ceiling was reached. Recognised on the way back in
#: as well as written on the way out: this state is rebuilt at every node, so a
#: notice counted as an ordinary unknown would push one real entry out of the
#: list on each transition for the rest of the run.
CAPACITY_NOTICE = "state_capacity_reached"


class InvestigationState(StrictModel):
    investigation_id: Annotated[str, Field(min_length=1, max_length=200)]
    request: RequestEnvelope
    parsed_request: ParsedRequest
    collection: dict[str, Any] | None = None

    hosts: Annotated[list[ResolvedHost], Field(max_length=20)] = Field(
        default_factory=list
    )
    unresolved_hosts: Annotated[list[str], Field(max_length=20)] = Field(
        default_factory=list
    )

    phenomenon: Annotated[str, Field(max_length=2000)] | None = None
    hypotheses: Annotated[list[Hypothesis], Field(max_length=20)] = Field(
        default_factory=list
    )
    known_facts: Annotated[list[KnownFact], Field(max_length=100)] = Field(
        default_factory=list
    )
    unknowns: Annotated[list[UnknownItem], Field(max_length=100)] = Field(
        default_factory=list
    )

    #: The questions of the current cycle and the calls that answer them, one
    #: call per question. A cycle used to hold exactly one of each, so four
    #: independent questions cost four cycles -- and a cycle is two model turns,
    #: twenty-six seconds, around a tool call that takes three tenths of one.
    #:
    #: The two dicts that used to sit here, keyed by tool name, are gone:
    #: arguments and host belong to the observation that asked for them, and
    #: keying by tool made two questions of the same tool impossible to tell
    #: apart.
    next_questions: Annotated[list[ObservationQuestion], Field(max_length=8)] = Field(
        default_factory=list,
    )
    planned_tool_calls: Annotated[list[PlannedToolCall], Field(max_length=8)] = Field(
        default_factory=list,
    )
    # Discovered once per investigation and checkpointed so every planning
    # turn sees one consistent set of live MCP contracts.
    tool_catalog: list[dict[str, Any]] = Field(default_factory=list)

    evidence: Annotated[list[Evidence], Field(max_length=200)] = Field(
        default_factory=list
    )
    #: What the last cycle's calls returned, in the order they were planned.
    last_observations: Annotated[
        list[ToolExecutionResult], Field(max_length=8)
    ] = Field(default_factory=list)
    tool_results: Annotated[list[ToolExecutionResult], Field(max_length=100)] = Field(
        default_factory=list,
    )
    tool_errors: Annotated[list[ToolExecutionResult], Field(max_length=100)] = Field(
        default_factory=list,
    )
    tool_call_purposes: dict[str, Annotated[str, Field(max_length=1000)]] = Field(
        default_factory=dict,
    )

    iteration_count: Annotated[int, Field(ge=0, le=20)] = 0
    tool_call_count: Annotated[int, Field(ge=0, le=100)] = 0
    limits: InvestigationLimits = Field(default_factory=InvestigationLimits)
    started_at: AwareDatetime
    stop_reason: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    limit_reached: bool = False
    fatal_error: Annotated[str, Field(min_length=1, max_length=10_000)] | None = None

    evidence_package: EvidencePackage | None = None

    #: The selected report template's output spec -- section ids, headings,
    #: which are required. Carried in state because the writer runs inside the
    #: graph now and the template was chosen before it started.
    template_output: dict[str, Any] | None = None
    report: Report | None = None
    #: What the checks said about the last draft, in the checker's own words.
    #: Handed back to the writer, which is the only way it learns that its own
    #: count or citation was rejected.
    report_findings: Annotated[list[str], Field(max_length=50)] = Field(
        default_factory=list,
    )
    #: Every finding that ever sent a draft back, kept across drafts.
    #: report_findings describes only the draft in hand and is cleared when it
    #: is replaced, so a rewrite that succeeded left no record of what was wrong
    #: -- and a check that is too eager would cost a second model call on every
    #: report with nothing naming it.
    report_rejections: Annotated[list[str], Field(max_length=100)] = Field(
        default_factory=list,
    )
    report_attempts: Annotated[int, Field(ge=0, le=10)] = 0
    #: Why the router turned the last plan away, so the planner can see what it
    #: got wrong instead of guessing again. A bad draft was allowed a second
    #: pass while a bad tool call ended the investigation outright, and a live
    #: run spent an entire investigation on one candidate that named a source
    #: where a host belonged. Cleared once a plan routes, because a rejection
    #: the planner has already answered is noise in the next payload; the
    #: permanent record of it is in unknowns.
    routing_rejections: Annotated[list[str], Field(max_length=20)] = Field(
        default_factory=list,
    )
    routing_attempts: Annotated[int, Field(ge=0, le=10)] = 0
    #: Summed across attempts, so the audit row reports the whole cost of
    #: writing rather than the last pass.
    report_duration_ms: Annotated[int, Field(ge=0)] = 0

    visited_nodes: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def bound_what_only_grows(cls, data: Any) -> Any:
        """Trim the accumulating lists, and say that the trim happened.

        Losing the hundred-and-first unknown costs a line in the limitations
        section. Raising over it costs the investigation, which is not a trade
        worth making for a list whose whole purpose is to record what went
        wrong. The notice takes the last slot so a reader is never shown a
        truncated account that does not admit it is truncated.

        What is kept is what was recorded first, so an entry that survived one
        node is not displaced by a later one -- and the notice says the ceiling
        rather than a running count, which is what lets rebuilding this state
        at the next node arrive at the same list instead of trimming again.
        """
        if not isinstance(data, Mapping):
            return data

        unknowns = [
            item for item in (data.get("unknowns") or []) if not _is_notice(item)
        ]
        facts = list(data.get("known_facts") or [])
        # One slot short, because the notice needs one of its own.
        over_unknowns = len(unknowns) > UNKNOWNS_CAPACITY - 1
        over_facts = len(facts) > KNOWN_FACTS_CAPACITY
        if not over_unknowns and not over_facts:
            # Already inside the ceiling. Any notice `data` carries stays where
            # it is rather than being rewritten, which is what makes rebuilding
            # this state a second time change nothing.
            return data

        full = []
        if over_unknowns:
            full.append("unknowns")
        if over_facts:
            full.append("confirmed facts")

        updated = dict(data)
        updated["known_facts"] = facts[:KNOWN_FACTS_CAPACITY]
        updated["unknowns"] = [
            *unknowns[: UNKNOWNS_CAPACITY - 1],
            UnknownItem(
                code=CAPACITY_NOTICE,
                message=(
                    "the investigation reached the "
                    f"{UNKNOWNS_CAPACITY} entries this state holds of "
                    + " and ".join(full)
                    + "; later ones were not recorded, so the report covers"
                    " only what is kept here"
                ),
            ),
        ]
        return updated

    @model_validator(mode="after")
    def validate_graph_invariants(self) -> "InvestigationState":
        # Keyed on the name because that is what the sources share. A host
        # found in a log search has no Zabbix id, and two hosts with no id
        # are not the same host.
        _unique([host.host for host in self.hosts], "host")
        hypothesis_ids = [hypothesis.id for hypothesis in self.hypotheses]
        _unique(hypothesis_ids, "hypothesis id")
        _unique([item.evidence_id for item in self.evidence], "evidence_id")
        _unique([item.tool_call_id for item in self.tool_results], "tool_call_id")

        known_hypotheses = set(hypothesis_ids)
        known_evidence = {item.evidence_id for item in self.evidence}
        referenced_evidence = {
            evidence_id
            for fact in self.known_facts
            for evidence_id in fact.evidence_ids
        }
        for hypothesis in self.hypotheses:
            referenced_evidence.update(hypothesis.supporting_evidence_ids)
            referenced_evidence.update(hypothesis.counter_evidence_ids)
        if missing_evidence := referenced_evidence - known_evidence:
            raise ValueError(
                f"known_facts reference unknown evidence: {sorted(missing_evidence)}"
            )
        for question in self.next_questions:
            missing = set(question.discriminates_hypothesis_ids) - known_hypotheses
            if missing:
                raise ValueError(
                    f"next_questions reference unknown hypotheses: {sorted(missing)}"
                )
        resolved_hosts = {host.host for host in self.hosts}
        for planned in self.planned_tool_calls:
            missing = set(planned.target_hypothesis_ids) - known_hypotheses
            if missing:
                raise ValueError(
                    f"planned_tool_calls reference unknown hypotheses: {sorted(missing)}"
                )
            if planned.host and planned.host not in resolved_hosts:
                raise ValueError("planned_tool_calls reference an unresolved host")
        if self.tool_call_count != len(self.tool_results):
            raise ValueError("tool_call_count must equal the number of tool_results")
        return self

    @property
    def declared_window_policy(self) -> str | None:
        """The query policy the selected report template asked for.

        Nothing read `collection.window.policy` at all, so it was a field an
        operator filled in and no code consulted: the monthly capacity report
        got the long-window policy only because its window happened to be long
        enough for the span rule in `apply_window_policy` to notice, and a
        template asking for it over a shorter window would have got nothing.

        A property on the state rather than a helper in one node module,
        because four call sites in two of them need it.
        """
        window = (self.collection or {}).get("window")
        if not isinstance(window, Mapping):
            return None
        policy = window.get("policy")
        return policy if isinstance(policy, str) else None

    def elapsed_seconds(self, now: datetime) -> float:
        return max(0, (now - self.started_at).total_seconds())


def _unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} values must be unique")


def _is_notice(item: Any) -> bool:
    """Whether this unknown is the ceiling notice rather than a finding.

    Reached with either shape: the nodes append `UnknownItem`s, and a state
    rebuilt from a checkpoint arrives as plain dicts.
    """
    if isinstance(item, UnknownItem):
        return item.code == CAPACITY_NOTICE
    if isinstance(item, Mapping):
        return item.get("code") == CAPACITY_NOTICE
    return False
