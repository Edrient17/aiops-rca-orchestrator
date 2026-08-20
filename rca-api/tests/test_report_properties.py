"""Checks that judge a report against its own evidence.

Every case here is a report that was actually produced and actually wrong, or
one that was produced and right and must not be accused. The second kind is the
harder half: two earlier versions of `counts_are_grounded` were discarded
because measuring them over a hundred and seventeen real reports showed they
either flagged nothing at all or flagged mostly correct answers.
"""

import pytest

from aiops_rca.evals import check_report
from aiops_rca.evals.properties import (
    counts_are_grounded,
    evidence_refs_resolve,
    omission_is_disclosed,
    required_sections_are_answered,
    unknowns_reach_limitations,
    unsupported_cause_is_admitted,
)


def _package(*evidence, unknowns=()):
    return {"evidence": list(evidence), "unknowns": list(unknowns)}


def _item(text, refs=("e1",)):
    return {
        "text": text,
        "label": None,
        "evidence_refs": list(refs),
        "counter_evidence_refs": [],
    }


def _report(section_id, *items, body=None):
    return {"sections": [{"id": section_id, "body": body, "items": list(items)}]}


TRIGGERS = {
    "evidence_id": "e1",
    "summary": "템플릿과 트리거: 1 rows {\"returned\":1}",
    "observed": {
        "kind": "rows",
        "omitted": 0,
        "items": [{"triggers": [{"triggerid": str(i)} for i in range(26)]}],
    },
}


class TestCountsAreGrounded:
    def test_a_miscount_is_caught(self):
        # The report said twenty-five. Zabbix returned twenty-six, nothing was
        # truncated, and the evidence carried all of them.
        findings = counts_are_grounded(
            _package(TRIGGERS), _report("answer", _item("정의된 트리거: 25개"))
        )
        assert [f.check for f in findings] == ["counts_are_grounded"]
        assert "25" in findings[0].detail

    def test_the_right_count_passes(self):
        assert counts_are_grounded(
            _package(TRIGGERS), _report("answer", _item("정의된 트리거: 26개"))
        ) == []

    def test_a_timestamp_does_not_ground_a_claim(self):
        # The first version scraped every integer out of every string, so
        # `09:25:14` grounded a claim of twenty-five and the check found nothing
        # in a hundred and seventeen reports.
        noisy = {**TRIGGERS, "summary": "read at 2026-08-20T09:25:14Z"}
        assert counts_are_grounded(
            _package(noisy), _report("answer", _item("트리거: 25개"))
        )

    def test_a_count_embedded_in_a_summary_grounds_a_claim(self):
        # When rows travel in `observed`, everything else the tool said is
        # folded into the summary as JSON text, so a real count can be a
        # substring. Reading key and value together is what separates this from
        # the timestamp above.
        evidence = {
            "evidence_id": "e1",
            "summary": '60 processes {"limit":500,"kernel_threads_omitted":94}',
            "observed": {"kind": "processes", "omitted": 0, "items": [{}] * 60},
        }
        assert counts_are_grounded(
            _package(evidence),
            _report("limitations", _item("kernel thread 94개를 제외했습니다")),
        ) == []

    def test_evidence_that_counts_nothing_abstains(self):
        # Before query_zabbix declared a list field its rows sat in prose, and a
        # correct "26개" had nothing countable behind it. Accusing there flagged
        # forty-two reports, most of them right.
        prose = {"evidence_id": "e1", "summary": "26 triggers, listed above"}
        assert counts_are_grounded(
            _package(prose), _report("answer", _item("트리거: 26개"))
        ) == []

    def test_an_uncited_claim_is_not_judged(self):
        assert counts_are_grounded(
            _package(TRIGGERS), _report("answer", _item("트리거: 25개", refs=()))
        ) == []

    def test_a_query_limit_is_a_count(self):
        # Live, a report stated the bound its query ran under -- row_limit 50 --
        # and was told the number came from nowhere. How many a query was
        # allowed to return is a fact about how many.
        limited = {
            "evidence_id": "e1",
            "summary": '1 rows {"data_quality":{"row_limit":50,"hit_row_limit":false}}',
        }
        assert counts_are_grounded(
            _package(limited), _report("limitations", _item("조회는 50건 한도였습니다"))
        ) == []

    def test_only_the_cited_evidence_counts(self):
        # Pooling counts across the whole package let an unrelated list vouch
        # for a number. The citation says what the claim rests on.
        other = {
            "evidence_id": "e2",
            "observed": {"kind": "ports", "omitted": 0, "items": [{}] * 25},
        }
        assert counts_are_grounded(
            _package(TRIGGERS, other), _report("answer", _item("트리거: 25개"))
        )


class TestEvidenceRefsResolve:
    def test_a_citation_to_nothing_is_caught(self):
        findings = evidence_refs_resolve(
            _package(TRIGGERS), _report("answer", _item("있다", refs=("e9",)))
        )
        assert [f.check for f in findings] == ["evidence_refs_resolve"]

    def test_a_real_citation_passes(self):
        assert evidence_refs_resolve(_package(TRIGGERS), _report("answer", _item("있다"))) == []


class TestOmissionIsDisclosed:
    def _cut(self, omitted=45, carried=15, extra=None):
        evidence = {
            "evidence_id": "e1",
            "summary": '15 processes {"returned": 60}',
            "observed": {
                "kind": "processes",
                "omitted": omitted,
                "items": [{}] * carried,
            },
        }
        if extra:
            evidence.update(extra)
        return evidence

    def test_a_shortened_list_presented_whole_is_caught(self):
        findings = omission_is_disclosed(
            _package(self._cut()),
            _report("processes", _item("실행 중인 프로세스: sshd, java")),
        )
        assert [f.check for f in findings] == ["omission_is_disclosed"]

    def test_a_disclosing_word_passes(self):
        assert omission_is_disclosed(
            _package(self._cut()),
            _report("processes", _item("일부만 표시했습니다: sshd, java")),
        ) == []

    def test_repeating_the_evidence_s_own_count_passes(self):
        # Live, the words were the whole test and it rejected a report that had
        # said "657건 매칭 중 100건만 반환되었으므로 … 완전히 판별할 수 없습니다"
        # twice -- neither "부분" nor "만 반환" was on the list. Passing on the
        # tool's own numbers is the disclosure, and a number is decidable where
        # a phrasing is not.
        assert omission_is_disclosed(
            _package(self._cut()),
            _report("processes", _item("60건 중 일부를 아래에 싣습니다")),
        ) == []

    def test_a_number_from_somewhere_else_does_not_count(self):
        assert omission_is_disclosed(
            _package(self._cut()),
            _report("processes", _item("트리거는 26개입니다")),
        )

    def test_a_shortened_list_nobody_cited_is_not_being_presented(self):
        assert omission_is_disclosed(
            _package(self._cut()),
            _report("processes", _item("다른 이야기", refs=())),
        ) == []

    def test_nothing_omitted_says_nothing(self):
        assert omission_is_disclosed(_package(TRIGGERS), _report("answer", _item("x"))) == []


class TestUnknownsReachLimitations:
    @pytest.mark.parametrize("stated", ["해당 없음", "없음", ""])
    def test_a_wall_reported_as_no_wall_is_caught(self, stated):
        package = _package(TRIGGERS, unknowns=[{"code": "x", "message": "y"}])
        report = _report("limitations", _item(stated) if stated else None)
        report["sections"][0]["items"] = [_item(stated)] if stated else []
        assert unknowns_reach_limitations(package, report)

    def test_a_stated_limitation_passes(self):
        package = _package(TRIGGERS, unknowns=[{"code": "x", "message": "y"}])
        report = _report("limitations", _item("템플릿 조회가 무시되었습니다"))
        assert unknowns_reach_limitations(package, report) == []

    def test_no_unknowns_says_nothing(self):
        assert unknowns_reach_limitations(
            _package(TRIGGERS), _report("limitations", _item("해당 없음"))
        ) == []


def test_check_report_runs_them_all():
    package = _package(TRIGGERS, unknowns=[{"code": "x", "message": "y"}])
    report = {
        "sections": [
            {
                "id": "answer",
                "body": None,
                "items": [_item("트리거: 25개", refs=("e1", "missing"))],
            },
            {"id": "limitations", "body": None, "items": [_item("해당 없음")]},
        ],
    }
    codes = {finding.check for finding in check_report(package, report)}
    assert codes == {
        "counts_are_grounded",
        "evidence_refs_resolve",
        "unknowns_reach_limitations",
    }


TEMPLATE = {
    "sections": [
        {"id": "answer", "heading": "확인 결과", "required": True},
        {"id": "notes", "heading": "참고", "required": False},
        {"id": "limitations", "heading": "분석 한계", "required": True},
    ],
}


def _sectioned(**bodies):
    return {
        "sections": [
            {"id": name, "body": body, "items": []} for name, body in bodies.items()
        ],
    }


class TestRequiredSectionsAreAnswered:
    """What replaces the coverage sweep.

    The sweep collected a section's declared evidence before the report was
    written, which guaranteed the section could be filled and cost a tool call
    on every run whether the question needed one or not. The guarantee moves to
    the end: leave a required section empty and say nothing about why, and the
    draft comes back.
    """

    def test_an_empty_required_section_with_no_stated_limit_is_caught(self):
        report = _sectioned(answer="", limitations="해당 없음")
        findings = required_sections_are_answered({}, report, TEMPLATE)
        assert [f.section_id for f in findings] == ["answer"]
        assert "확인 결과" in findings[0].detail

    def test_saying_what_was_missing_passes(self):
        report = _sectioned(answer="", limitations="Wazuh 에이전트가 응답하지 않았습니다")
        assert required_sections_are_answered({}, report, TEMPLATE) == []

    def test_a_filled_section_passes(self):
        report = _sectioned(answer="트리거 26개", limitations="해당 없음")
        assert required_sections_are_answered({}, report, TEMPLATE) == []

    def test_an_optional_section_may_be_empty_and_unexplained(self):
        report = _sectioned(answer="답", notes="", limitations="해당 없음")
        assert required_sections_are_answered({}, report, TEMPLATE) == []

    def test_a_report_with_no_sections_abstains(self):
        # Reports written before the current writer had another shape entirely.
        # Measured over real history, accusing them flagged every required
        # section of every one -- an absence of structure is not an absence of
        # answer.
        assert required_sections_are_answered({}, {"impact": {}}, TEMPLATE) == []

    def test_no_template_abstains(self):
        assert required_sections_are_answered({}, _sectioned(answer=""), None) == []


class TestUnsupportedCauseIsAdmitted:
    """An investigation that supported no explanation says so.

    Asked about templates and triggers, a report offered three hypotheses, none
    of them supported, and wrote "해당 없음" under 분석 한계. Not finding a cause is
    a legitimate answer; reporting no limits while holding none is not.
    """

    def _package(self, *statuses):
        return {"hypotheses": [{"id": f"H{i}", "status": s} for i, s in enumerate(statuses)]}

    def test_all_rejected_with_no_stated_limit_is_caught(self):
        report = _sectioned(limitations="해당 없음")
        findings = unsupported_cause_is_admitted(
            self._package("rejected", "unresolved", "rejected"), report, TEMPLATE
        )
        assert len(findings) == 1
        assert "3" in findings[0].detail

    def test_one_supported_hypothesis_passes(self):
        report = _sectioned(limitations="해당 없음")
        assert unsupported_cause_is_admitted(
            self._package("rejected", "supported"), report, TEMPLATE
        ) == []

    def test_stating_the_limit_passes(self):
        report = _sectioned(limitations="원인을 특정하지 못했습니다")
        assert unsupported_cause_is_admitted(
            self._package("rejected", "rejected"), report, TEMPLATE
        ) == []

    def test_an_investigation_with_no_hypotheses_abstains(self):
        # A host-state question never forms one. Demanding a limitation there
        # would flag every report of a kind that does not reason causally.
        report = _sectioned(limitations="해당 없음")
        assert unsupported_cause_is_admitted({"hypotheses": []}, report, TEMPLATE) == []
