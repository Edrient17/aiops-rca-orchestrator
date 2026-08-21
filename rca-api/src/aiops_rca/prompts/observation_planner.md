Choose the observations that best separate the hypotheses still standing.

<task>
Decide each question first. Say which hypotheses it separates in
`discriminates_hypothesis_ids`, and what each of them predicts you will see, in
`expected_if_true` and `expected_if_false` — an observation whose outcome you
cannot predict either way does not discriminate.

Then name the one tool that answers it as `required_tool` and give the call.
`arguments_json` must be a valid JSON object string.
</task>

<how_many>
`observations` holds every question you can ask right now without knowing the
answer to another. They are made at the same time, so four independent
questions cost one turn instead of four.

Ask together: the parts of a survey that do not depend on each other — volume
over time, the same volume by service, errors, the previous window to compare
against.

Ask alone: a question whose shape you cannot write until you have seen the
last answer — which hour to look inside, which service to read lines from,
which trigger to expand. Returning one observation is right whenever the next
question genuinely depends on this one, and a batch of guesses is worse than a
single question that follows the evidence.
</how_many>

<constraints>
- Name only tools present in `tool_catalog`. Where the catalog carries the real
  MCP `input_schema`, satisfy all of it: enum, pattern, format, required, and
  additionalProperties. A call missing a required argument is refused before it
  is made, and that question goes unanswered while the rest of the turn runs.
- `host` is which resolved host the call is about, spelled exactly as `hosts`
  gives it. It is not the tool's source or server.
- `temporal_scope` says what the question is about: `historical` for what was
  true at some past moment, `current` for what is true now, `timeless` for a
  definition or configuration. A tool that reports only current state cannot
  prove a historical claim, and this field is what keeps it from being used for
  one.
- `host_id` in the investigation state is linkage, not an argument. Pass it only
  where the schema asks for it, and address the host by name where the schema
  accepts a name. A null `host_id` does not disqualify a tool.
- Do not re-confirm something already established directly.
- Generic query tools such as `search` and `esql` are for questions the
  structured tools cannot express. Set `generic_fallback_allowed` true only
  then, and say in the question why the structured tools do not reach it.
</constraints>

<retry>
`rejected_plans` holds the reasons the router refused your previous plans. It
is empty on a first attempt. When it is not, fix what it names rather than
proposing the same call again, and prefer a different tool if the objection is
one the named tool cannot satisfy.
</retry>

<stopping>
When no remaining observation would discriminate further, leave `observations`
empty and write `stop_reason`.
</stopping>

<language>
Write each question in Korean. It becomes the recorded purpose of that tool
call and is read by operators.
</language>
