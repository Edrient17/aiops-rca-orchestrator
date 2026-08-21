Choose the single next observation that best separates the hypotheses still
standing.

<task>
Decide the question first. Say which hypotheses it separates in
`discriminates_hypothesis_ids`, and what each of them predicts you will see, in
`expected_if_true` and `expected_if_false` — an observation whose outcome you
cannot predict either way does not discriminate.

Then name the one tool that answers it as `required_tool`, and give the call in
`candidates`. `arguments_json` must be a valid JSON object string.
</task>

<constraints>
- Name only tools present in `tool_catalog`. Where the catalog carries the real
  MCP `input_schema`, satisfy all of it: enum, pattern, format, required, and
  additionalProperties.
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

<stopping>
When no remaining observation would discriminate further, set `question` and
`required_tool` to null and write `stop_reason`.
</stopping>

<language>
Write the question in Korean. It becomes the recorded purpose of the tool call
and is read by operators.
</language>
