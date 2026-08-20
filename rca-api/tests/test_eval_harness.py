"""Loading real history, and scoring what the writer makes of it.

The dataset is built from investigations this service actually ran, which means
it spans schema versions and report kinds that no longer exist. Deciding what to
do with those is most of the work here: an example that cannot be loaded is not
a failure to report on every run, and an example silently dropped is a dataset
whose size nobody can account for.
"""

import asyncio
import json
import re
from typing import Any

import pytest

from aiops_rca.evals.harness import evaluators, load_examples, writer_target
from aiops_rca.evals.properties import counts_are_grounded

PACKAGE = {
    "schema_version": "0.1.0",
    "request": {
        "request_id": "REQ-1",
        "original_question": "트리거가 몇 개야",
        "requested_by": "U1",
    },
    "query_context": {
        "hosts": [{"host": "vm-java-docker-2", "host_id": "11094"}],
        "timezone": "Asia/Seoul",
        "anchor_time": "2026-08-20T00:00:00Z",
    },
    "investigation": {
        "initial_window": {"from": "2026-08-19T00:00:00Z", "to": "2026-08-20T00:00:00Z"},
        "final_window": {"from": "2026-08-19T00:00:00Z", "to": "2026-08-20T00:00:00Z"},
        "iterations": 1,
        "tool_calls": [],
        "expansion_reasons": [],
        "stop_reason": "done",
        "limit_reached": False,
    },
    "observed_failure_mode": "확인 요청",
    "confirmed_facts": [],
    "hypotheses": [],
    "evidence": [],
    "unknowns": [],
}

PARSED = {
    "schema_version": "0.1.0",
    "request_id": "REQ-1",
    "original_question": "트리거가 몇 개야",
    "parse_status": "ready",
    "request_type": "host_state_check",
    "user_intent": "확인",
    "host_queries": ["vm-java-docker-2"],
    "timezone": "Asia/Seoul",
    "anchor_time": "2026-08-20T00:00:00Z",
    "initial_window_hint": None,
    "incident_type_hint": None,
    "incident_description": "확인 요청",
    "allow_dynamic_expansion": True,
    "ambiguities": [],
}

TEMPLATE_OUTPUT = {"guidance": "", "sections": [{"id": "answer", "required": True}]}


def _row(**overrides: Any) -> str:
    row = {
        "request_id": "REQ-1",
        "question": "트리거가 몇 개야",
        "parsed": PARSED,
        "package": PACKAGE,
        "report": {"sections": []},
        "template_output": TEMPLATE_OUTPUT,
    }
    row.update(overrides)
    return json.dumps(row, ensure_ascii=False)


def _write(tmp_path, *rows: str):
    path = tmp_path / "export.jsonl"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


class TestLoadingHistory:
    def test_a_usable_row_loads(self, tmp_path):
        loaded = load_examples(_write(tmp_path, _row()))
        assert len(loaded) == 1
        assert loaded.examples[0].request_id == "REQ-1"
        assert loaded.skipped == {}

    def test_a_retired_report_kind_is_counted_not_raised(self, tmp_path):
        # request_type left the templates directory, so the join found nothing.
        # Thirty-five of a hundred and seventeen real rows are like this.
        loaded = load_examples(_write(tmp_path, _row(template_output=None)))
        assert len(loaded) == 0
        assert loaded.skipped == {"report kind no longer exists": 1}

    def test_an_older_schema_is_counted_not_raised(self, tmp_path):
        loaded = load_examples(_write(tmp_path, _row(package={"schema_version": "0.0.1"})))
        assert loaded.skipped == {"predates the current schema": 1}

    def test_the_usable_rows_survive_the_unusable_ones(self, tmp_path):
        # The point of counting rather than raising: one bad row must not cost
        # the dataset.
        path = _write(tmp_path, _row(template_output=None), _row(request_id="REQ-2"))
        loaded = load_examples(path)
        assert [item.request_id for item in loaded.examples] == ["REQ-2"]
        assert sum(loaded.skipped.values()) == 1

    def test_blank_lines_are_not_rows(self, tmp_path):
        path = tmp_path / "export.jsonl"
        path.write_text(f"\n{_row()}\n\n", encoding="utf-8")
        assert len(load_examples(path)) == 1

    def test_inputs_carry_what_the_writer_needs(self, tmp_path):
        inputs = load_examples(_write(tmp_path, _row())).examples[0].as_inputs()
        assert set(inputs) == {
            "request_id",
            "question",
            "parsed",
            "package",
            "template_output",
        }


class TestScoring:
    def _score(self, package, report):
        evaluator = next(
            item for item in evaluators() if item.__name__ == "counts_are_grounded"
        )
        return evaluator(inputs={"package": package}, outputs={"report": report})

    def test_a_clean_report_scores_one(self):
        assert self._score(PACKAGE, {"sections": []})["score"] == 1

    def test_a_finding_scores_zero_and_says_why(self):
        package = {
            "evidence": [
                {
                    "evidence_id": "e1",
                    "observed": {"kind": "rows", "omitted": 0, "items": [{}] * 26},
                },
            ],
        }
        report = {
            "sections": [
                {
                    "id": "answer",
                    "items": [
                        {
                            "text": "트리거 25개",
                            "evidence_refs": ["e1"],
                            "counter_evidence_refs": [],
                        },
                    ],
                },
            ],
        }
        result = self._score(package, report)
        assert result["score"] == 0
        assert "25" in result["comment"]

    def test_abstaining_scores_one_rather_than_vanishing(self):
        # Omitting the score would make an experiment's average rise whenever
        # the evidence stopped being checkable, which is backwards.
        package = {"evidence": [{"evidence_id": "e1", "summary": "26 triggers"}]}
        report = {
            "sections": [
                {
                    "id": "answer",
                    "items": [
                        {
                            "text": "트리거 26개",
                            "evidence_refs": ["e1"],
                            "counter_evidence_refs": [],
                        },
                    ],
                },
            ],
        }
        assert counts_are_grounded(package, report) == []
        assert self._score(package, report)["score"] == 1

    def test_every_check_becomes_an_evaluator_under_its_own_name(self):
        names = [item.__name__ for item in evaluators()]
        assert "counts_are_grounded" in names
        assert len(names) == len(set(names))


class TestTheWriterTarget:
    def test_it_runs_the_writer_on_stored_evidence(self, tmp_path):
        seen: dict[str, Any] = {}

        class StubModel:
            async def complete(self, **kwargs: Any) -> Any:
                seen.update(kwargs)
                return kwargs["output_type"].model_validate(
                    {"title": "t", "sections": [{"id": "answer", "items": []}]},
                )

        example = load_examples(_write(tmp_path, _row())).examples[0]
        target = writer_target(StubModel(), "stub-model")
        output = asyncio.run(target(example.as_inputs()))

        assert seen["model"] == "stub-model"
        # The package goes in as it was stored, not re-collected.
        assert seen["payload"]["evidence_package"]["request"]["request_id"] == "REQ-1"
        assert output["report"]["sections"][0]["id"] == "answer"

    def test_a_writer_failure_is_not_swallowed(self, tmp_path):
        class Failing:
            async def complete(self, **_: Any) -> Any:
                raise RuntimeError("model refused")

        example = load_examples(_write(tmp_path, _row())).examples[0]
        with pytest.raises(RuntimeError):
            asyncio.run(writer_target(Failing(), "stub")(example.as_inputs()))


class TestTheEvaluateCommand:
    """The command that spends money, exercised without spending any.

    `evaluate` deferred its imports into the function body, so nothing checked
    them: it named `configure_tracing` where the module exports `configure`, and
    the mistake surfaced as an ImportError on the VM, after a container rebuild
    and a dataset copy. Every other path here had a test; this one had the cost
    of running it as an excuse not to.
    """

    def _run(self, monkeypatch, tmp_path, examples=3):
        import aiops_rca.evals.run as run

        seen: dict[str, Any] = {}

        class FakeClient:
            def list_examples(self, **kwargs: Any) -> list[Any]:
                seen["dataset"] = kwargs.get("dataset_name")
                return [object()] * examples

        class FakeResults:
            experiment_name = "writer-test"

            async def __aiter__(self):
                for score, comment in ((1, "ok"), (0, "claims 25개")):
                    yield {
                        "example": type(
                            "E", (), {"inputs": {"request_id": "REQ-1"}, "id": 1}
                        )(),
                        "evaluation_results": {
                            "results": [
                                type(
                                    "R",
                                    (),
                                    {
                                        "key": "counts_are_grounded",
                                        "score": score,
                                        "comment": comment,
                                    },
                                )(),
                            ],
                        },
                    }

        async def fake_aevaluate(target: Any, **kwargs: Any) -> FakeResults:
            seen["target"] = target
            seen["evaluators"] = kwargs["evaluators"]
            seen["data"] = kwargs["data"]
            return FakeResults()

        monkeypatch.setattr("langsmith.Client", lambda *a, **k: FakeClient())
        monkeypatch.setattr("langsmith.aevaluate", fake_aevaluate)
        monkeypatch.setattr(
            "aiops_rca.services.llm.OpenAIStructuredModel",
            lambda **kwargs: object(),
        )
        monkeypatch.setattr("aiops_rca.services.tracing.configure", lambda _: False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("AIOPS_INTERNAL_TOKEN", "x" * 24)
        # Read off the source table rather than listed here, so a fourth MCP
        # server does not quietly break this test into a false red.
        from aiops_rca.sources import SOURCES

        for profile in SOURCES.values():
            monkeypatch.setenv(profile.url_setting.upper(), "http://localhost:1")
            if profile.token_setting:
                monkeypatch.setenv(profile.token_setting.upper(), "token")
        return run, seen

    def test_it_reaches_the_experiment(self, monkeypatch, tmp_path):
        run, seen = self._run(monkeypatch, tmp_path)
        run.evaluate(_write(tmp_path, _row()), None)
        assert seen["dataset"] == run.DATASET_NAME
        # The writer is async all the way down, which is why this goes through
        # aevaluate at all -- the synchronous entry point refuses a coroutine
        # target, and a stub that accepted one hid that until the VM ran it.
        assert asyncio.iscoroutinefunction(seen["target"])
        assert [item.__name__ for item in seen["evaluators"]] == [
            "evidence_refs_resolve",
            "counts_are_grounded",
            "omission_is_disclosed",
            "unknowns_reach_limitations",
        ]

    def test_it_reports_the_score_rather_than_an_object(self, monkeypatch, tmp_path, capsys):
        # The command printed the results object's repr, which answered nothing
        # about whether the writer had got better or worse.
        run, _ = self._run(monkeypatch, tmp_path)
        run.evaluate(_write(tmp_path, _row()), None)
        printed = capsys.readouterr().out
        assert "writer-test" in printed
        # Padding is cosmetic; the pair is not.
        assert re.search(r"counts_are_grounded\s+1/2", printed)
        # A failing case names itself, or the number is a dead end.
        assert "REQ-1" in printed
        assert "claims 25개" in printed

    def test_limit_is_honoured_before_the_model_is_called(self, monkeypatch, tmp_path):
        # The flag exists so a first run costs five writer calls rather than
        # seventy-eight. Applying it after the fact would defeat the point.
        run, seen = self._run(monkeypatch, tmp_path, examples=20)
        run.evaluate(_write(tmp_path, _row()), 5)
        assert len(seen["data"]) == 5
