Write an operational RCA report from the Evidence Package, and from nothing
else.

<task>
Use the section ids and instructions the template gives. Answer each section
with what the Package holds. Keep three things apart and say which is which:
what was established, what failure was observed, and what might have caused it
along with what to do about it.
</task>

<item_label>
`label` is a short Korean tag naming what that one item is about — 변동 구간,
수집 단절, 비교 대상, 기준선 한계. Two to six characters of the subject, not a
classification of the item: 관측인지 가설인지는 문장이 말한다.

Write it once, in `label`. `text` begins with the sentence itself — not with a
bracketed tag, and not with a classifying word and a colon. A line that opens
`[OBSERVED_FAILURE] [변동 구간]` or `[자료한계] 한계:` says the same thing
twice, and in the first case one of the two is wrong, since a peak in log
volume is not a failure.

Whether something is observed, inferred or recommended is carried by how the
sentence is written and by the section it sits in. Say it there.
</item_label>

<constraints>
- Do not add, rename, reorder or drop a section.
- `evidence_refs` and `counter_evidence_refs` may cite only ids that exist in
  the Package. Do not invent, reshape or guess one.
- Do not supply a fact from outside the Package, and do not recompute a metric
  it already carries. If you cannot support a statement from the Package, do
  not make it.
- A section marked `evidence_unavailable` means that observation was never
  collected. Do not leave it blank and do not answer it anyway: say what is
  missing and what could not be concluded without it.
- Where the Package reports a limit, a truncation or a gap, carry it into the
  report. A count taken from a truncated result is a floor, not a total, and
  has to read as one.
</constraints>

<language>
Write the report in Korean.
</language>
