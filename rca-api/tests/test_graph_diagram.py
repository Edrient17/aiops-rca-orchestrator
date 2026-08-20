"""The picture of the graph has to be the graph.

A diagram is worth having only if being wrong is impossible rather than
unlikely. This compares the edges drawn in `docs/collector-graph.md` against the
edges of the compiled graph, so a node added or a route changed without
regenerating the document fails here instead of quietly leaving a diagram that
describes last month's pipeline.

Edges are compared, not the file as a whole: LangGraph writes its own styling
into the mermaid, and a cosmetic change in a dependency should not read as a
change to the pipeline.
"""

import re
from pathlib import Path

from aiops_rca.graph.diagram import (
    collector_graph,
    collector_graph_document,
    collector_graph_mermaid,
)

DOCUMENT = Path(__file__).resolve().parents[1] / "docs" / "collector-graph.md"

# `a --> b;` and the dashed conditional form `a -.-> b;`
_EDGE = re.compile(r"^\s*(\w+)\s*-(\.?)->\s*(\w+);", re.MULTILINE)


def _drawn_edges(text: str) -> set[tuple[str, str, bool]]:
    return {
        (source, target, bool(dot))
        for source, dot, target in _EDGE.findall(text)
    }


def _compiled_edges() -> set[tuple[str, str, bool]]:
    return {
        (edge.source, edge.target, edge.conditional)
        for edge in collector_graph().get_graph().edges
    }


def test_the_document_draws_the_compiled_graph():
    assert _drawn_edges(DOCUMENT.read_text(encoding="utf-8")) == _compiled_edges()


def test_the_document_is_what_the_generator_writes():
    # Beyond the edges: a stale heading or a lost preamble is still a document
    # nobody regenerated.
    assert DOCUMENT.read_text(encoding="utf-8") == collector_graph_document()


def test_every_node_is_reachable_from_the_start():
    # A node drawn but never entered is a node that does nothing, and the
    # diagram would show it sitting there looking like part of the pipeline.
    edges = _compiled_edges()
    reached = {"__start__"}
    frontier = ["__start__"]
    while frontier:
        node = frontier.pop()
        for source, target, _ in edges:
            if source == node and target not in reached:
                reached.add(target)
                frontier.append(target)
    assert reached == set(collector_graph().get_graph().nodes)


def test_drawing_it_needs_no_credentials_or_network():
    # The reason this is a test and not a screenshot. If drawing ever starts
    # requiring a model client or an MCP session, this stops being checkable on
    # every run and the diagram goes back to being someone's chore.
    assert "graph TD" in collector_graph_mermaid()
