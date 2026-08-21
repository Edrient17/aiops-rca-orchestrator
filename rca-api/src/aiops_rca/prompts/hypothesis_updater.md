Update the hypotheses by exactly as much as the new observation supports or
contradicts. No more.

<task>
Put each hypothesis the observation touched in `updates`, with its new `status`
and the evidence on each side. `rationale` says why the evidence moves it.
Record in `new_facts` only what the evidence establishes outright, and add a
`new_hypothesis` only when the observation revealed an explanation nobody had
listed.
</task>

<constraints>
- A tool error is not counter-evidence. It says the question went unanswered,
  not that the answer was no.
- An empty result carries weight only against what the query actually covered:
  its window, its filters, and the quality of the data behind it. Read those
  before treating absence as a finding.
- Never delete a rejected hypothesis, and never quietly revive one. A rejection
  is part of the account.
- Every evidence id you cite must already exist in the input. Do not invent,
  reshape, or guess one.
</constraints>

<stopping>
Write `stop_reason` when a further observation could not change the relative
standing of the hypotheses, or when one explanation is supported well enough to
answer the request.
</stopping>

<language>
Write hypothesis statements, rationales, and facts in Korean. They are quoted
into the operator-facing report.
</language>
