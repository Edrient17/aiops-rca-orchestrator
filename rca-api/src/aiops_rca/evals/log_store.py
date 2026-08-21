"""Derive, or re-check, what the prompts claim about the log store.

`log_queries.md` tells three planning nodes which indices exist, which fields
carry the host and the message, and which of three ways of matching text
actually works here. Every line of it was measured against a live cluster --
and a measurement goes stale. This deployment has three hosts and a demo index
template; a real one will have neither, and `host.name` in particular inverts
between an ECS mapping and this one.

A stale prompt is worse than no prompt. Without one the model gropes; with one
it confidently names a field that is not there. So this is two commands:

    probe   what the store actually looks like, formatted to write the file from
    check   whether the file's claims still hold, non-zero exit when they do not

`check` reads the claims out of the prompt rather than keeping its own copy of
them, because a second copy is the thing that goes stale first.
"""

import argparse
import asyncio
import json
import re
from importlib.resources import files
from typing import Any

from aiops_rca.config.settings import Settings
from aiops_rca.services.investigation import build_live_service
from aiops_rca.tools.registry import RoutingContext

PROMPT = "log_queries.md"
GATE = RoutingContext(generic_fallback_allowed=True)

#: The three ways of asking the same question. Which one wins is the finding;
#: that they disagree at all is the reason the prompt exists.
FORMS = {
    "MATCH(message, ...)": 'MATCH(message, "{term}")',
    "message LIKE": 'message LIKE "*{term}*"',
    "message.keyword LIKE": 'message.keyword LIKE "*{term}*"',
}


def _rows(response: Any) -> list[dict[str, Any]]:
    """The rows out of a reply that arrives as prose with JSON stuck on the end."""
    text = response if isinstance(response, str) else json.dumps(response, default=str)
    start = text.find("[")
    if start < 0:
        return []
    try:
        parsed = json.loads(text[start:])
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def claims(text: str) -> tuple[list[str], list[str]]:
    """The index patterns and field names the prompt commits to.

    Read out of the prose so the prompt stays the only place they are written.
    Anything in backticks that looks like a dotted field or a wildcard index;
    ES|QL keywords are upper case and fall out on their own.
    """
    quoted = set(re.findall(r"`([^`]+)`", text))
    patterns = sorted(item for item in quoted if "*" in item and " " not in item)
    fields = sorted(
        item
        for item in quoted
        if re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", item)
    )
    return patterns, fields


def examples(text: str) -> list[str]:
    """The ES|QL the prompt holds up as the way to ask.

    An example that no longer runs is worse than no example: the planner copies
    its shape and loses the turn. One live investigation lost its error counts
    to `CASE(MATCH(...))` inside an `EVAL`, which ES|QL refuses, so the examples
    are checked by running them rather than by reading them.
    """
    found: list[str] = []
    block: list[str] = []
    for line in text.splitlines():
        if line.startswith("    ") and line.strip():
            block.append(line.strip())
            continue
        if block:
            joined = " ".join(block)
            if joined.startswith("FROM "):
                found.append(joined)
            block = []
    if block and " ".join(block).startswith("FROM "):
        found.append(" ".join(block))
    return found


class Store:
    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    async def esql(self, query: str) -> tuple[str, list[dict[str, Any]], str | None]:
        result = await self.adapter.execute("esql", {"query": query}, GATE)
        return result.status, _rows(result.response), result.error

    async def indices(self, pattern: str) -> list[dict[str, Any]]:
        result = await self.adapter.execute(
            "list_indices", {"index_pattern": pattern}, GATE
        )
        return _rows(result.response)

    async def columns(self, pattern: str) -> list[str]:
        _status, rows, _error = await self.esql(f"FROM {pattern} | LIMIT 1")
        return sorted(rows[0]) if rows else []

    async def counts(self, pattern: str, term: str, window: str) -> dict[str, int]:
        found: dict[str, int] = {}
        for label, form in FORMS.items():
            where = f"@timestamp > NOW() - {window} AND {form.format(term=term)}"
            status, rows, error = await self.esql(
                f"FROM {pattern} | WHERE {where} | STATS n = COUNT(*)"
            )
            found[label] = (
                int(rows[0].get("n", 0)) if status == "ok" and rows else -1
            )
            if error:
                found[label] = -1
        return found


async def probe(store: Store, pattern: str, term: str, window: str) -> None:
    indices = await store.indices(pattern)
    print(f"indices matching {pattern}: {len(indices)}")
    for row in indices[:5]:
        print(f"   {row.get('index')}  docs={row.get('docs.count')}")
    if len(indices) > 5:
        print(f"   ... {len(indices) - 5} more")

    columns = await store.columns(pattern)
    plain = [name for name in columns if not name.endswith(".keyword")]
    print(f"\ncolumns: {len(columns)} ({len(plain)} before .keyword variants)")
    for name in plain:
        print(f"   {name}")

    print(f"\nmatching {term!r} over the last {window}:")
    for label, count in (await store.counts(pattern, term, window)).items():
        print(f"   {count:>8}  {label}" + ("   (query failed)" if count < 0 else ""))
    print(
        "\nWrite the winning form into the prompt, and the losing counts beside"
        "\nit -- the numbers are what make the trap legible."
    )


async def check(store: Store, text: str, term: str, window: str) -> list[str]:
    patterns, fields = claims(text)
    failures: list[str] = []
    if not patterns:
        return ["the prompt names no index pattern to check"]

    for pattern in patterns:
        if not await store.indices(pattern):
            failures.append(f"{pattern} matches no index")
            continue
        columns = set(await store.columns(pattern))
        if not columns:
            failures.append(f"{pattern} returned no rows to read fields from")
            continue
        for field in fields:
            if field not in columns:
                failures.append(f"{pattern} has no field {field}")

        counts = await store.counts(pattern, term, window)
        print(f"{pattern}: {json.dumps(counts)}")
        best = max(counts, key=lambda label: counts[label])
        if not best.startswith("MATCH"):
            failures.append(
                f"{pattern}: the prompt says MATCH finds the most, but "
                f"{best} did ({json.dumps(counts)})"
            )
        if len(set(counts.values())) == 1:
            failures.append(
                f"{pattern}: the three forms now agree, so the warning the "
                f"prompt spends its tokens on no longer describes this store"
            )

    for query in examples(text):
        status, _rows_out, error = await store.esql(query)
        print(f"example: {status:<6} {query[:70]}")
        if status != "ok":
            failures.append(f"example no longer runs ({error}): {query}")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["probe", "check"])
    parser.add_argument("--pattern", default="vm-logs-*", help="probe only")
    parser.add_argument("--term", default="ERROR")
    parser.add_argument("--window", default="24 hours")
    args = parser.parse_args()

    service = build_live_service(Settings())
    store = Store(service.adapters.for_source("elasticsearch"))
    text = (files("aiops_rca.prompts") / PROMPT).read_text(encoding="utf-8")

    if args.command == "probe":
        asyncio.run(probe(store, args.pattern, args.term, args.window))
        return

    failures = asyncio.run(check(store, text, args.term, args.window))
    if failures:
        print(f"\n{PROMPT} no longer describes this store:")
        for item in failures:
            print(f"   {item}")
        raise SystemExit(1)
    print(f"\n{PROMPT} still holds")


if __name__ == "__main__":
    main()
