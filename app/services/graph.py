"""Graph VIEW — the app-facing call-site + flow shape, from the ONE grouping brain (connectors/graph.py).

There is a single graph builder: the connector's tree-folder (`source.pull_graph`), which resolves node
identity from the run tree, splits a node by prompt shape (ReAct pre/post-tool are different call-sites),
and computes real node->node edges. This module does NOT re-group; it just reshapes that builder's output
into the neutral dict the app services (funnel, downgrade, audit, cli) render — so no connector type leaks
upward and every consumer keeps one stable shape.

  nodes = LLM call-sites with core stats (model, calls, avg tokens, graph_path, out_type)
  edges = observed node->node flow (most-travelled path)
  graphs = distinct subgraph paths (select / rollup by graph for multi-agent)
"""
from connectors.datasource import get_source


def build(source_id, ws_id, project, limit=300):
    """Fold an agent's traces into {agent, nodes, edges, graphs} via the one rich builder. Read-only, no LLM."""
    g, skipped = get_source(source_id).pull_graph(ws_id, project, limit=limit)
    nodes = []
    for n in g.nodes:
        nodes.append({
            "node": n["node"], "key": n["key"], "variant": n.get("variant", ""),
            "calls": n["samples"], "traces": n["traces"],
            "avg_in": n["avg_in"], "avg_out": n["avg_out"],
            "model": n["model"],
            "models": n.get("models_list") or [{"name": n["model"], "calls": n["samples"]}],
            "mixed": n.get("mixed_model", False),
            "graph_path": n.get("graph_path", ""),          # subgraph nesting — select / rollup by graph
            "out_type": n.get("out_type"),                  # deterministic output structure (json/short/free-form)
            "untagged": n.get("untagged", False),
        })
    nodes.sort(key=lambda x: -x["avg_out"])
    graphs = sorted({n["graph_path"] for n in nodes if n["graph_path"]})
    return {"agent": g.agent, "nodes": nodes, "edges": g.edges, "graphs": graphs, "skipped": len(skipped)}
