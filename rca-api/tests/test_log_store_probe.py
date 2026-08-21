"""Reading the prompt's claims back out of the prompt.

`check` verifies what `log_queries.md` says against a live cluster. What it
must not do is keep its own copy of the claims -- a second copy is the thing
that goes stale first, which is how a table of required arguments outlived the
server it described.
"""

from aiops_rca.evals.log_store import FORMS, _rows, claims


class TestWhatTheCheckReadsFromThePrompt:
    def test_it_finds_the_index_pattern_and_the_fields(self):
        text = "query them as `vm-logs-*`; the host is `host.name`, also `host.hostname`."
        patterns, fields = claims(text)
        assert patterns == ["vm-logs-*"]
        assert fields == ["host.hostname", "host.name"]

    def test_esql_keywords_are_not_mistaken_for_fields(self):
        # They are upper case and undotted, so they fall out without a list of
        # them to maintain.
        _patterns, fields = claims("`STATS`, `COUNT(*)`, `MATCH`, `BUCKET`")
        assert fields == []

    def test_the_live_prompt_still_names_something_to_check(self):
        from importlib.resources import files

        text = (files("aiops_rca.prompts") / "log_queries.md").read_text(
            encoding="utf-8"
        )
        patterns, fields = claims(text)
        assert patterns, "nothing for check to verify means check verifies nothing"
        assert "host.name" in fields


class TestReadingAReplyThatIsProseWithJsonOnTheEnd:
    def test_it_takes_the_rows(self):
        assert _rows('Results\n[{"n": 3}]') == [{"n": 3}]

    def test_a_reply_with_no_rows_is_empty_rather_than_an_error(self):
        # This runs against whatever a future cluster returns, and a crash here
        # would read as "the store is wrong" instead of "the reply changed".
        assert _rows("Results\n[]") == []
        assert _rows("no json here") == []
        assert _rows("Results\n[broken") == []


def test_all_three_matching_forms_are_still_compared():
    # The finding is that they disagree. Dropping one would let the prompt keep
    # recommending a form nothing measures any more.
    assert len(FORMS) == 3
    assert any(name.startswith("MATCH") for name in FORMS)
