<log_store>
Logs live in daily indices named `vm-logs-YYYY.MM.DD`. Query them together as
`vm-logs-*`. The store holds a few hundred thousand documents per day, so an
unfiltered query returns far more than anyone can read.

The log line is in `message`; `event.original` is the same text. There is no
parsed level field — `INFO`, `WARN`, `ERROR` are words inside `message`.
`service.name` is empty in this deployment; which service wrote a line is in
`log.file.path` (`/hostfs/var/log/msa-demo/payment-service.log`) and in the
`[name]` prefix of the line itself. The host is `host.name`, also `host.hostname`.
</log_store>

<matching_text>
`message` is an analysed field, and the three ways of matching it disagree.
Measured over one 24-hour window in this deployment:

    WHERE MATCH(message, "ERROR")         ->  15
    WHERE message LIKE "*ERROR*"          ->   0
    WHERE message.keyword LIKE "*ERROR*"  ->   4   (whole store, not 24h)

Use `MATCH` for text. `message.keyword` holds nothing for any line past the
keyword length limit, which is most of them, and `LIKE` against the analysed
field misses without saying so. Both failures return a number, and a zero from
either is indistinguishable from "nothing happened".

Short identifiers behave the other way. In `esql`, `STATS ... BY host.name`
groups on the exact name with no `MATCH` needed.

In the `search` DSL the same field needs care for the opposite reason: it is
analysed, so a `term` query on it returns nothing, and a loose `query_string`
pulls in other hosts whose names share a token — asking for
`vm-java-docker-2` came back with `test-java-docker-vm`. Use `match`, or
`host.name.keyword`, and never `term`.
</matching_text>

<counts_before_documents>
`esql` aggregates. `search` returns whole documents and one call of it can cost
more than the rest of an investigation together.

A question about how many, how often, or when is a `STATS`, and the answer is a
few rows:

    FROM vm-logs-* | WHERE @timestamp > NOW() - 24 hours AND MATCH(message, "ERROR")
    | STATS n = COUNT(*) BY log.file.path, host.name | SORT n DESC | LIMIT 5

    FROM vm-logs-* | WHERE @timestamp > NOW() - 6 hours
    | STATS n = COUNT(*) BY bucket = BUCKET(@timestamp, 1 hour), host.name
    | SORT bucket

Read documents only when the wording of a line is itself the evidence — a stack
trace, an exact error. Narrow with `STATS` first so you know which lines to ask
for, then take them with `LIMIT`.
</counts_before_documents>
