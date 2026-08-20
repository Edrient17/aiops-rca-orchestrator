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
    unknowns_reach_limitations,
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
    def _cut(self):
        return {
            "evidence_id": "e1",
            "observed": {"kind": "processes", "omitted": 45, "items": [{}] * 15},
        }

    def test_a_shortened_list_presented_whole_is_caught(self):
        findings = omission_is_disclosed(
            _package(self._cut()),
            _report("processes", _item("실행 중인 프로세스: sshd, java")),
        )
        assert [f.check for f in findings] == ["omission_is_disclosed"]

    def test_saying_so_passes(self):
        assert omission_is_disclosed(
            _package(self._cut()),
            _report("processes", _item("일부만 표시했습니다: sshd, java")),
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
