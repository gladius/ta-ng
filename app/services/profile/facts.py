"""Per-node FACT-PACK — the deterministic ($0, no LLM) ground truth the analyst reasons over.

Two variants (a node's type decides which):
  LLM node  -> reads the contract trace (system / output / messages): the 6 facts + flow-context.
  non-LLM   -> tool/retriever structural facts (type/name/calls/error/latency). Its NAME is its op — no analyst.

Modularity: this CONSUMES the shared graph (`app/services/graph.build`: nodes, structural_nodes, buckets, edges)
+ the profile sampler's `bucket_facts`. It OWNS only the profile-specific facts (frame-stripped distinct-output,
role-shape, rag-present, flow-context). Nothing here calls an LLM, touches a lever, or lives outside `profile/`.
"""
import re

from app.services.profile.select import bucket_facts

_NUM = re.compile(r"\d+")
_ART = re.compile(r"\[\d+\]")            # a retrieved-doc article marker, e.g. "[1] ..."


def _norm(t):
    """Mask numbers/ids so a TEMPLATED output ('order 123' vs 'order 456') collapses to one structural form —
    the fix for distinct-output mistaking a templated node for a free-form one."""
    return _NUM.sub("#", (t or "")).strip().lower()


def _fs_distinct(texts):
    """Frame-stripped distinct count: mask ids, then drop lines byte-identical across ALL calls, count the
    distinct remainders. Low ⇒ decision/templated node; high ⇒ genuinely free-form."""
    norms = [_norm(x) for x in texts]
    if len(norms) > 1:
        common = set.intersection(*[set(n.split("\n")) for n in norms])
        norms = ["\n".join(l for l in n.split("\n") if l not in common) for n in norms]
    return len(set(norms))                # an all-empty remainder = ONE templated form (not zero)


def _out_shape(o):
    o = (o or "").strip()
    if not o:
        return "empty"
    if o[:1] in "{[":
        return "json"
    if len(o.split()) <= 4 and "\n" not in o:
        return "label"
    return "prose"


def _shape_mix(outputs):
    """Distribution of output SHAPES over ALL N (not a sample) — a $0 mixed-bucket signal that catches even a
    RARE minority mode, because it sees every call (a 5-sample read can't). Returns (shapes, is_mixed). Structural
    only: two prose modes of different MEANING look the same here — that residual still needs the read."""
    from collections import Counter
    c = Counter(_out_shape(o) for o in outputs)
    c.pop("empty", None)
    top = c.most_common()
    mixed = len(top) >= 2 and top[1][1] >= 2          # 2nd shape present in >=2 calls -> catches ~2% minorities
    return dict(c), mixed


def _roles(t):
    return [m.get("role") for m in t.get("input_messages", [])]


def _structure(bucket):
    """Role-shape signals over the bucket: multi-turn, tool-result present, retrieved-docs present. Deterministic."""
    n = max(1, len(bucket))
    multi = sum(1 for t in bucket if _roles(t).count("user") > 1 or "assistant" in _roles(t))
    tool_res = sum(1 for t in bucket if "tool" in _roles(t))
    rag = sum(1 for t in bucket
              if any(_ART.search(m.get("content") or "") or "retrieved" in (m.get("content") or "").lower()
                     for m in t.get("input_messages", []) if m.get("role") == "user"))
    return {"multi_turn": multi > n * 0.5, "has_tool_result": tool_res > 0, "rag_present": rag > n * 0.5}


def _tools_called(bucket):
    return sorted({name for t in bucket for name in (t.get("tools_called") or [])})


def _flow(key, edges):
    """Immediate graph neighbours (node names) from the observed edges — $0 context: a node fed by a router is a
    worker. Empty on flat (tree-less) sources."""
    up = sorted({e["src"].rsplit("/", 1)[-1] for e in edges if e.get("dst") == key and e.get("src") != key})
    down = sorted({e["dst"].rsplit("/", 1)[-1] for e in edges if e.get("src") == key and e.get("dst") != key})
    return {"upstream": up, "downstream": down}


def llm_facts(node, bucket, edges):
    """The LLM 6-fact pack + flow-context for one call-site. All deterministic, $0."""
    bf = bucket_facts(bucket) if bucket else {"distinct_system": 0, "n_total": 0}
    st = _structure(bucket)
    outputs = [str(t.get("output") or "") for t in bucket]
    shapes, mixed_shapes = _shape_mix(outputs)
    return {
        "kind": "llm", "node": node["node"], "key": node["key"], "graph_path": node.get("graph_path", ""),
        "n": bf.get("n_total", node.get("calls", 0)),
        "distinct_system": bf.get("distinct_system", 0),          # is the frame fixed?
        "output_type": node.get("out_type"),                      # label / json / prose
        "distinct_output": _fs_distinct(outputs),                 # frame-stripped decision cardinality
        "output_shapes": shapes, "mixed_shapes": mixed_shapes,    # shape distribution over ALL N -> mixed signal
        "tool_rate": node.get("tool_input_rate", 0), "avg_tools": node.get("avg_tools", 0),
        "tools": _tools_called(bucket),
        "multi_turn": st["multi_turn"], "has_tool_result": st["has_tool_result"],
        "rag_present": st["rag_present"],
        "flow": _flow(node["key"], edges),
        "untagged": node.get("untagged", False), "mixed_model": node.get("mixed", False),
        "error_rate": node.get("error_rate", 0.0),
    }


def nonllm_facts(snode, edges):
    """The non-LLM structural pack. The NAME is the op — deterministic, no analyst, no lever."""
    return {
        "kind": snode["type"], "node": snode["node"], "key": snode["key"],
        "graph_path": snode.get("graph_path", ""),
        "op": snode["node"],                                      # name IS the op
        "calls": snode.get("calls", 0), "error_rate": snode.get("error_rate", 0.0),
        "avg_ms": snode.get("avg_ms", 0),
        "flow": _flow(snode["key"], edges),
        # distinct-arg / distinct-result: DEFERRED — needs the ADAPTER to capture tool inputs/outputs (a connector
        # change), not present in the current structural record. Flagged, not faked.
        "arg_result_variability": None,
    }


def fact_pack(g):
    """graph dict -> {node_key: fact-pack} for every node (LLM 6-fact + flow · non-LLM structural). $0, no LLM."""
    edges, buckets = g.get("edges", []), g.get("buckets", {})
    out = {}
    for n in g.get("nodes", []):
        out[n["key"]] = llm_facts(n, buckets.get(n["key"], []), edges)
    for s in g.get("structural_nodes", []):
        out[s["key"]] = nonllm_facts(s, edges)
    return out
