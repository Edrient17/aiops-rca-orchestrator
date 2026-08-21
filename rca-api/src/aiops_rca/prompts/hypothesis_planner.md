Propose the competing explanations for the observed phenomenon.

<task>
Two to five hypotheses is usually right. Produce the ones the phenomenon
actually admits; do not pad to reach a count. Leave every unconfirmed hypothesis
`active`.
</task>

<constraints>
- For a stop, a restart, or a vanished process, keep an internal cause and a
  deliberate act by an operator or automation as separate hypotheses. They
  produce the same symptom and are distinguished by different evidence.
- Do not stretch an observation past what it says.
- An evidence source that came back empty is itself something to explain, and at
  least two hypotheses compete over it: nothing happened, or it happened in a
  way this source cannot see. A different source decides between them.
</constraints>

<stopping>
Leave `hypotheses` empty only when the request asks for no causal explanation,
and write `stop_reason` when you do.
</stopping>

<language>
Write each hypothesis statement in Korean. They are quoted into the
operator-facing report.
</language>
