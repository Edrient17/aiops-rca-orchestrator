"""What must hold between a report and the evidence it was written from.

The unit tests prove the machinery is right. Nothing proved the *answers* were
right, and every defect found this month was found by a person reading a Slack
message: a host with twenty-six triggers reported as twenty-five, a host with
two templates reported as having none, sixty processes presented as fifteen
without saying so. All of them passed the whole suite.

None of these checks needs to know the right answer, which is what makes them
usable against live infrastructure whose right answer changes hourly. They ask
whether the report is consistent with its own evidence -- and every defect above
was an inconsistency of exactly that kind.
"""

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: A number written against one of these is a claim about how many, and a claim
#: about how many is answerable from the evidence rather than from memory.
#:
#: The digits may be grouped, and reports group them: `7,261건`. Matching bare
#: digits read that as 261, which nothing counted, so the check rejected a
#: sentence that was right and cost the investigation a second draft -- half
#: its wall clock -- and handed the writer a finding that was not true.
_COUNTED = re.compile(r"(\d[\d,]*)\s*(개|건|대|회|가지|개소|줄)")

_NO_LIMITS = ("해당 없음", "없음", "특이사항 없음", "N/A", "none")

_DISCLOSURES = ("생략", "잘림", "일부", "전체가 아", "omitted", "truncat")


@dataclass(frozen=True)
class Finding:
    check: str
    detail: str
    section_id: str | None = None


#: A check reads three documents: the evidence, the report, and the template
#: the report was written against. Most ignore the third; the two that judge
#: whether a report is *complete* cannot be written without it.
Check = Callable[
    [Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], list[Finding]
]


def _sections(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    sections = report.get("sections")
    if not isinstance(sections, list):
        return []
    return [item for item in sections if isinstance(item, Mapping)]


def _texts(section: Mapping[str, Any]) -> Iterator[str]:
    body = section.get("body")
    if isinstance(body, str) and body:
        yield body
    items = section.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                yield item["text"]


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, Mapping):
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            yield from _walk(nested)


#: Fields whose value is a count rather than an identifier or a measurement.
#: An id is a number too, and treating one as a quantity is how a claim of
#: twenty-five found support in a timestamp.
_COUNT_FIELDS = frozenset(
    {
        "returned",
        "count",
        "total",
        "omitted",
        "window_total",
        "sample_count",
        "iterations",
        "matched",
        "kernel_threads_omitted",
        # The bound a query ran under is a fact about how many, and a report
        # that states it correctly was being told the number came from nowhere.
        "row_limit",
        "limit",
    },
)


#: `"returned":154` inside a summary sentence. When rows travel in `observed`,
#: everything else the tool said is folded into the summary as JSON text, so a
#: count can be a substring rather than a field. Read as key and value together,
#: never as a loose number -- that is the difference between finding
#: kernel_threads_omitted and finding the 25 in a timestamp.
_EMBEDDED_COUNT = re.compile(r'"(\w+)"\s*:\s*(\d+)')


def _counted_quantities(value: Any, field: str | None = None) -> set[int]:
    """Every number the evidence offers as an answer to "how many"."""
    if isinstance(value, bool):
        return set()
    if isinstance(value, int):
        return {value} if field in _COUNT_FIELDS else set()
    if isinstance(value, str):
        return {
            int(number)
            for key, number in _EMBEDDED_COUNT.findall(value)
            if key in _COUNT_FIELDS
        }
    if isinstance(value, Mapping):
        found: set[int] = set()
        # select_counts maps a field name to its length, so its values are
        # counts whatever those names happen to be.
        if field == "select_counts":
            found.update(item for item in value.values() if isinstance(item, int))
        for key, nested in value.items():
            found |= _counted_quantities(nested, key)
        return found
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        found = {len(value)}
        for nested in value:
            found |= _counted_quantities(nested, field)
        return found
    return set()


def _evidence_items(package: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    evidence = package.get("evidence")
    if not isinstance(evidence, list):
        return []
    return [item for item in evidence if isinstance(item, Mapping)]


def evidence_refs_resolve(
    package: Mapping[str, Any],
    report: Mapping[str, Any],
    template: Mapping[str, Any] | None = None,
) -> list[Finding]:
    """Every citation names evidence that exists.

    A reference to an id the package does not hold is a footnote to nothing, and
    a reader cannot tell it apart from one that leads somewhere.
    """
    known = {
        item["evidence_id"]
        for item in _evidence_items(package)
        if isinstance(item.get("evidence_id"), str)
    }
    findings: list[Finding] = []
    for section in _sections(report):
        for item in section.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            for field in ("evidence_refs", "counter_evidence_refs"):
                for ref in item.get(field) or []:
                    if ref in known:
                        continue
                    findings.append(
                        Finding(
                            check="evidence_refs_resolve",
                            detail=f"{field} names {ref!r}, absent from the package",
                            section_id=str(section.get("id")),
                        ),
                    )
    return findings


def counts_are_grounded(
    package: Mapping[str, Any],
    report: Mapping[str, Any],
    template: Mapping[str, Any] | None = None,
) -> list[Finding]:
    """A stated count appears in the evidence.

    Asked how many triggers a host had, a report answered twenty-five. The
    evidence held twenty-six, none of it truncated anywhere in the pipeline.
    Counting a list of twenty-six is not something to ask of a reader at the far
    end of a pipeline that already knows the length.

    A claim is judged only against **the evidence it cites**, and only when that
    evidence counts something -- the length of a list it carries, or a field
    whose name means a count. Anything else abstains.

    Two rounds of measurement against a hundred and seventeen real reports
    produced that shape. Grounding written as any integer anywhere in the
    package flagged nothing at all, the true defect included, because a package
    full of trigger ids and timestamps contains almost every small integer
    somewhere -- `09:25:14` grounded a claim of twenty-five. Narrowed to counts
    but still pooled across the whole package, it flagged forty-two reports, and
    most were correct answers whose evidence happened to carry its rows in prose
    rather than in a list: a true "26개" looked baseless because nothing in that
    package counted to twenty-six.

    The citation is what makes it decidable. An item says which evidence backs
    it, so that evidence is what the number has to be true of.
    """
    by_id = {
        item["evidence_id"]: item
        for item in _evidence_items(package)
        if isinstance(item.get("evidence_id"), str)
    }

    findings: list[Finding] = []
    for section in _sections(report):
        for item in section.get("items") or []:
            if not isinstance(item, Mapping) or not isinstance(item.get("text"), str):
                continue
            claims = _COUNTED.findall(item["text"])
            if not claims:
                continue
            cited = [by_id[ref] for ref in item.get("evidence_refs") or [] if ref in by_id]
            grounded: set[int] = set()
            for evidence in cited:
                grounded |= _counted_quantities(evidence)
            if not grounded:
                # Nothing cited here counts anything, so the claim cannot be
                # judged. An absence is not evidence of a defect.
                continue
            for number, unit in claims:
                if int(number.replace(",", "")) in grounded:
                    continue
                findings.append(
                    Finding(
                        check="counts_are_grounded",
                        detail=(
                            f"claims {number}{unit}; the evidence it cites counts "
                            f"{sorted(grounded)}"
                        ),
                        section_id=str(section.get("id")),
                    ),
                )
    return findings


def omission_is_disclosed(
    package: Mapping[str, Any],
    report: Mapping[str, Any],
    template: Mapping[str, Any] | None = None,
) -> list[Finding]:
    """A list the evidence had to shorten is not presented as the whole list.

    Sixty services arrived as fifteen and the report described those fifteen as
    what the host was running. The evidence says how many it left behind; the
    report has to pass that on, or the reader takes a prefix for the set.

    Disclosure was a list of words -- 생략, 잘림, 일부, truncated. Live, that
    rejected a report which had said "657건 매칭 중 100건만 반환되었으므로 …
    완전히 판별할 수 없습니다" twice, in two different sections, because neither
    "부분" nor "만 반환" was on the list. It cost a rewrite and then shipped a
    finding against a report that had done exactly the right thing.

    Repeating the evidence's own counts *is* the disclosure, and a number is
    decidable where a phrasing is not. Only cited evidence is judged: a
    shortened list nobody quoted is not being presented as anything.
    """
    cited: set[str] = set()
    for section in _sections(report):
        for item in section.get("items") or []:
            if isinstance(item, Mapping):
                cited.update(item.get("evidence_refs") or [])

    shortened = [
        item
        for item in _evidence_items(package)
        if item.get("evidence_id") in cited
        and isinstance(item.get("observed"), Mapping)
        and isinstance(item["observed"].get("omitted"), int)
        and item["observed"]["omitted"] > 0
    ]
    if not shortened:
        return []

    whole = " ".join(text for section in _sections(report) for text in _texts(section))
    if any(word in whole for word in _DISCLOSURES):
        return []
    stated = {int(found) for found in re.findall(r"\d{1,9}", whole)}

    findings: list[Finding] = []
    for evidence in shortened:
        if _counted_quantities(evidence) & stated:
            continue
        findings.append(
            Finding(
                check="omission_is_disclosed",
                detail=(
                    f"{evidence['observed']['omitted']} rows were left out of "
                    f"{evidence.get('evidence_id')} and the report repeats none "
                    f"of its counts"
                ),
            ),
        )
    return findings


def unknowns_reach_limitations(
    package: Mapping[str, Any],
    report: Mapping[str, Any],
    template: Mapping[str, Any] | None = None,
) -> list[Finding]:
    """An investigation that hit a wall does not report having hit none.

    The limitations section is where a reader learns what the answer does not
    cover. Written as "해당 없음" while the package carries unknowns, it removes
    the one signal that would have made the gap visible.
    """
    unknowns = package.get("unknowns")
    if not isinstance(unknowns, list) or not unknowns:
        return []
    for section in _sections(report):
        if str(section.get("id")) != "limitations":
            continue
        stated = " ".join(_texts(section)).strip()
        if stated and stated not in _NO_LIMITS:
            return []
        shown = stated or "(empty)"
        return [
            Finding(
                check="unknowns_reach_limitations",
                detail=f"{len(unknowns)} unknowns recorded, limitations says {shown!r}",
                section_id="limitations",
            ),
        ]
    return []


def _limitations(report: Mapping[str, Any]) -> str:
    for section in _sections(report):
        if str(section.get("id")) == "limitations":
            return " ".join(_texts(section)).strip()
    return ""


def _stated(text: str) -> bool:
    return bool(text) and text not in _NO_LIMITS


def required_sections_are_answered(
    package: Mapping[str, Any],
    report: Mapping[str, Any],
    template: Mapping[str, Any] | None = None,
) -> list[Finding]:
    """A section the template insists on is filled, or its absence is explained.

    This is what replaces the coverage sweep. That node read the effects each
    section declared and collected them before the report was written, which
    guaranteed the sections could be filled and cost a tool call on every run
    whether the question needed one or not -- a request about templates still
    paid for a process query.

    The guarantee moves to the end. An empty required section is not forbidden;
    going out with nothing said about the investigation's limits is.

    Written strictly first -- the limitations had to name the empty section's
    own heading -- it flagged fourteen of seventy-eight real reports, and they
    were right. A report finding no anomaly has no candidate causes to list, and
    the formatter already renders a required section that arrived empty as
    "해당 없음", so nobody was misled. What it could not tell apart is a section
    empty because there was nothing to say from one empty because the
    investigation never looked, and only the second is a defect.

    So the bar is the weaker, decidable one: leave a required section empty and
    the report has to acknowledge some limit. The finding still names the
    section, which is what makes it something a second draft can act on.
    """
    sections = template.get("sections") if isinstance(template, Mapping) else None
    if not isinstance(sections, list):
        return []
    if not _sections(report):
        # A report with no sections at all is not a report this can read. The
        # reports written before the current writer had a different shape
        # entirely, and measured over real history this accused every one of
        # them of leaving every required section empty. An absence of structure
        # is not an absence of answer.
        return []

    filled = {
        str(section.get("id"))
        for section in _sections(report)
        if any(_texts(section))
    }
    if _stated(_limitations(report)):
        return []

    findings: list[Finding] = []
    for declared in sections:
        if not isinstance(declared, Mapping) or not declared.get("required"):
            continue
        section_id = str(declared.get("id"))
        if section_id in filled or section_id == "limitations":
            continue
        heading = str(declared.get("heading") or section_id)
        findings.append(
            Finding(
                check="required_sections_are_answered",
                detail=(
                    f"{heading!r} is required and empty, and the report states "
                    f"no limitation"
                ),
                section_id=section_id,
            ),
        )
    return findings


def unsupported_cause_is_admitted(
    package: Mapping[str, Any],
    report: Mapping[str, Any],
    template: Mapping[str, Any] | None = None,
) -> list[Finding]:
    """An investigation that supported no explanation says so.

    The hypotheses carry their own verdict. When every one of them was rejected
    or left unresolved, the investigation did not find a cause -- which is a
    legitimate outcome and a useful answer. What is not legitimate is a report
    that reads as though it had found one, with nothing under 분석 한계.

    What this can decide is narrow, and worth being plain about: there is no
    link from a sentence in the report to a hypothesis, so it cannot tell that
    *this* claim is unbacked. It can tell that nothing was supported and the
    report claimed no limits, which is the shape the failure takes.
    """
    hypotheses = package.get("hypotheses")
    if not isinstance(hypotheses, list) or not hypotheses:
        return []
    if any(
        isinstance(item, Mapping) and item.get("status") == "supported"
        for item in hypotheses
    ):
        return []
    if _stated(_limitations(report)):
        return []
    return [
        Finding(
            check="unsupported_cause_is_admitted",
            detail=(
                f"none of the {len(hypotheses)} hypotheses was supported and the "
                f"report states no limitation"
            ),
            section_id="limitations",
        ),
    ]


CHECKS: tuple[Check, ...] = (
    evidence_refs_resolve,
    counts_are_grounded,
    omission_is_disclosed,
    unknowns_reach_limitations,
    required_sections_are_answered,
    unsupported_cause_is_admitted,
)


def check_report(
    package: Mapping[str, Any],
    report: Mapping[str, Any],
    template: Mapping[str, Any] | None = None,
    checks: tuple[Check, ...] = CHECKS,
) -> list[Finding]:
    spec = template or {}
    return [finding for check in checks for finding in check(package, report, spec)]
