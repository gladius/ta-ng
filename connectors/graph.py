"""The execution-graph layer — the spine the auditor now reasons over.

A recorded trace is a TREE of runs (chain -> chain -> llm/tool). This module folds N such trees (the
recent runs of ONE agent) into a single agent execution graph, and derives the CALL-SITES the profiler
audits from that structure — instead of the old flat "group llm runs by a metadata label" heuristic.

Why structural, not label-based (both proven to matter on real LangGraph traces):
  - A ReAct node emits >1 llm call under the SAME node (reason-call vs post-tool-call). They are
    DIFFERENT prompts / different optimization targets, so they must be different call-sites. We split
    them by an input-SHAPE signature (does the prompt already contain a tool result? how many turns?).
  - An untagged / plain-LangChain call has no node label; grouping by run.name collapses every step into
    one "ChatAnthropic" bucket. We instead resolve the node from the run TREE (nearest named parent
    chain run), and only fall back to run.name as a last resort (flagged `untagged`).

Input:  an iterable of RUN RECORDS (neutral shape, produced by each adapter's to_record):
    {id, trace_id, parent_run_id, dotted_order, run_type, name, start_time, end_time,
     agent_hint, node_hint, trace}          # `trace` = a contract trace for llm runs, else None
Output: a Graph with
    .buckets  OrderedDict[call_site_key -> list[contract trace]]   (what profiler/audit consume)
    .nodes    [{key, node, variant, samples, traces, untagged, avg_in, avg_out, avg_ms, model, first, last}]
    .edges    [{src, dst, count}]                                  (observed node->node transitions)
Timing + volume are captured now (avg_ms, traces, first/last) so analytics + a richer visual view can be
built on top later without another schema change.
"""

import re
from collections import Counter, OrderedDict

from contract import eligible                          # error-free filter: a failed run is not a valid decision path

# chain-run names that are structural scaffolding, NOT a meaningful node identity
_GENERIC = {"", "langgraph", "runnablesequence", "runnableparallel", "runnablelambda",
            "runnableassign", "__start__", "__end__", "channelwrite", "_write", "_route"}


def _is_generic(name):
    return str(name or "").strip().lower() in _GENERIC


def _pct(values, q):
    """The q-quantile of a list (nearest-rank), 0 if empty — for the output-length distribution (p50/p95)."""
    if not values:
        return 0
    xs = sorted(values)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _is_json(text):
    """Does the recorded output parse as JSON (fence-tolerant)? A deterministic 'structured output' signal — no LLM."""
    import json
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1].rsplit("```", 1)[0].strip() if "\n" in s else s.strip("`")
    if not s[:1] in "{[":
        return False
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def _shape_sig(trace):
    """A compact signature of the prompt's STRUCTURE — the role sequence — so structurally different calls
    at the same node (pre-tool vs post-tool) don't merge. Content is irrelevant here; shape is."""
    return tuple(m["role"] for m in trace.get("input_messages", []))


def _frame_variants(items, min_prefix=6, min_frac=0.2, min_cooccur=0.5):
    """Split a node's NO-TOOL calls into distinct call-sites ONLY when they carry genuinely different
    authored call-TYPES (e.g. a 'Plan ...' call and a 'Critique ...' call). Two guards, BOTH required, so a
    map / fan-out node is never fragmented:

      (a) RE-SENT frame — a cluster counts only if its shared long prefix recurs across >= min_frac of the
          node's calls. A unique input (a distinct RAG question graded once) is a singleton, NOT a call-type.
      (b) CO-OCCURRENCE — >= 2 such real clusters appear together WITHIN one trace (plan+critique in the same
          run). Variable content that merely differs across traces (a reformulated question re-graded, a new
          topic each run) never co-occurs, so it stays ONE call-site.

    Returns {id: frame_slug or ''}. items = [(id, trace_id, user_text)]."""
    n = len(items)
    if n < 2:
        return {i: "" for i, _, _ in items}
    # 1) cluster by shared leading WORD-prefix (sorting brings same-frame calls adjacent; the running prefix
    #    shrinks to the shared authored frame — a distinct frame breaks the prefix and starts a new cluster).
    clusters, cluster_of = [], {}
    for k in sorted(range(len(items)), key=lambda x: items[x][2] or ""):
        cid, _tid, text = items[k]
        w = (text or "").split()
        if clusters:
            p = clusters[-1]["prefix"]
            j = 0
            while j < len(p) and j < len(w) and p[j] == w[j]:
                j += 1
            if j >= min_prefix:
                clusters[-1]["prefix"] = p[:j]; clusters[-1]["members"].append(cid)
                cluster_of[cid] = clusters[-1]["idx"]; continue
        cluster_of[cid] = len(clusters)
        clusters.append({"idx": len(clusters), "prefix": list(w), "members": [cid]})
    # 2) (a) keep only RE-SENT frames — a real call-type recurs; a one-off input is not one.
    real = {c["idx"] for c in clusters if len(c["members"]) >= max(2, min_frac * n) and len(c["prefix"]) >= min_prefix}
    if len(real) < 2:
        return {i: "" for i, _, _ in items}
    # 3) (b) co-occurrence of REAL clusters within one trace (plan+critique) vs one-cluster-per-trace (map/fan-out).
    per_trace, calls_per_trace = {}, {}
    for cid, tid, _ in items:
        calls_per_trace[tid] = calls_per_trace.get(tid, 0) + 1
        if cluster_of[cid] in real:
            per_trace.setdefault(tid, set()).add(cluster_of[cid])
    multi_call = [t for t, c in calls_per_trace.items() if c >= 2]
    cooccur = [t for t in multi_call if len(per_trace.get(t, ())) >= 2]
    if not multi_call or len(cooccur) < min_cooccur * len(multi_call):
        return {i: "" for i, _, _ in items}
    # 4) label ONLY real clusters by their frame slug; one-off calls fall back to the base bucket ('').
    slug = {}
    for c in clusters:
        if c["idx"] in real:
            slug[c["idx"]] = re.sub(r"[^a-z0-9]+", "", "".join(x.lower() for x in c["prefix"][:3]))[:14] or ("f%d" % c["idx"])
    return {cid: slug.get(cluster_of[cid], "") for cid, _, _ in items}


def _resolve_node(rec, by_id):
    """(node_label, tagged) for an llm run: metadata node -> nearest named parent chain -> run.name.

    Returns tagged=False only when we had to fall back to the model class name (run.name) — that call-site
    is 'untagged' and the graph flags it so the UI can warn instead of silently collapsing.
    """
    if rec.get("node_hint"):
        return str(rec["node_hint"]), True
    # walk up the parent chain to the nearest chain run with a real name
    seen, cur = set(), rec.get("parent_run_id")
    while cur and cur not in seen:
        seen.add(cur)
        p = by_id.get(str(cur))
        if p is None:
            break
        if p.get("node_hint"):
            return str(p["node_hint"]), True
        if p.get("run_type") == "chain" and not _is_generic(p.get("name")):
            return str(p["name"]), True
        cur = p.get("parent_run_id")
    return str(rec.get("name") or "llm"), False          # last resort: the model class name (untagged)


def _resolve_path(rec, by_id):
    """The graph/subgraph nesting for an llm run — the chain of NAMED ancestor nodes (outer -> inner), EXCLUDING
    the run's own node. '' at top level. Framework-agnostic: derived from the run TREE, so multiagent subgraphs
    group correctly even when LangGraph's checkpoint_ns metadata is absent. Used only as a fallback when the trace
    doesn't already carry a graph_path."""
    path, seen, cur = [], set(), rec.get("parent_run_id")
    while cur and str(cur) not in seen:
        seen.add(str(cur))
        p = by_id.get(str(cur))
        if p is None:
            break
        name = p.get("node_hint") or (p.get("name") if p.get("run_type") == "chain" else None)
        if name and not _is_generic(name):
            path.append(str(name))
        cur = p.get("parent_run_id")
    return "/".join(reversed(path))


class Graph:
    def __init__(self, agent, buckets, nodes, edges, trace_count):
        self.agent = agent
        self.buckets = buckets                           # OrderedDict[key -> list[trace]]  (downstream API)
        self.nodes = nodes
        self.edges = edges
        self.trace_count = trace_count


def build_graph(records, agent_default="agent"):
    """Fold run-records into one agent graph. Pure + deterministic; no I/O, no LLM."""
    records = [r for r in records if r]
    agent = next((r["agent_hint"] for r in records if r.get("agent_hint")), None) or agent_default

    # group by trace so tree-walks + per-trace node ordering stay within one execution
    by_trace = OrderedDict()
    for r in records:
        by_trace.setdefault(str(r.get("trace_id") or ""), []).append(r)

    # PASS 1 — resolve every llm run to (node_label, tagged, shape_sig); collect the shape variants per node
    resolved = []                                        # (rec, node_label, tagged, sig)
    paths = {}                                           # rec id -> tree-derived graph/subgraph path (fallback)
    node_order_per_trace = OrderedDict()                 # trace_id -> [node_label in first-seen dotted order]
    for tid, runs in by_trace.items():
        by_id = {str(r["id"]): r for r in runs if r.get("id")}
        order = []
        for rec in sorted(runs, key=lambda r: (r.get("dotted_order") or "", r.get("start_time") or "")):
            node = None
            elig_sample = True
            if rec.get("run_type") == "chain" and not _is_generic(rec.get("name")):
                node = rec.get("node_hint") or rec.get("name")
            if rec.get("trace") is not None and not rec.get("skip"):      # a profileable llm sample
                nl, tagged = _resolve_node(rec, by_id)
                paths[str(rec.get("id"))] = _resolve_path(rec, by_id)     # subgraph nesting from the tree (fallback)
                sig = _shape_sig(rec["trace"])
                resolved.append((rec, nl, tagged, sig))               # kept for stats + error_rate (all samples)
                node = node or nl
                elig_sample = eligible(rec["trace"])[0]               # but only SUCCESSFUL samples define the path
            if node and elig_sample and (not order or order[-1] != node):
                order.append(node)
        node_order_per_trace[tid] = order

    # FRAME SUB-SPLIT — a node's NO-TOOL calls that carry DISTINCT authored frames co-occurring in one trace
    # (plan+critique) are DIFFERENT call-sites; the binary tool/no-tool variant alone merges them, polluting
    # the bucket's static/dynamic + output signals. Map/fan-out nodes (one frame, variable fill) are untouched.
    no_tool_items = {}
    for rec, nl, tagged, sig in resolved:
        if "tool" not in sig:
            ut = " ".join(m.get("content", "") for m in rec["trace"].get("input_messages", []) if m.get("role") == "user")
            no_tool_items.setdefault(nl, []).append((rec.get("id"), rec.get("trace_id") or "", ut))
    frame_label = {}
    for nl, items in no_tool_items.items():
        frame_label.update(_frame_variants(items))

    def _full_variant(rec, sig):
        """The call-site variant: ReAct tool split (after-tool) OR the frame slug for a distinct no-tool
        call-type; '' (base bucket) for a plain single-frame node."""
        if "tool" in sig:
            return "after-tool"
        return frame_label.get(rec.get("id"), "") or "initial"

    variants_per_node = {}                               # node_label -> set(full variant) — decides where to split
    for rec, nl, tagged, sig in resolved:
        variants_per_node.setdefault(nl, set()).add(_full_variant(rec, sig))

    # PASS 2 — assign each sample a call-site key: 'agent/node', + '~variant' only where the node has >1
    buckets = OrderedDict()
    stats = OrderedDict()                                # key -> accumulator
    for rec, nl, tagged, sig in resolved:
        multi = len(variants_per_node.get(nl, ())) > 1
        variant = _full_variant(rec, sig) if multi else ""
        key = "%s/%s%s" % (agent, nl, ("~" + variant) if variant else "")   # '~' = URL-safe unreserved
        buckets.setdefault(key, []).append(rec["trace"])
        s = stats.setdefault(key, {"node": nl, "variant": variant, "untagged": not tagged,
                                   "in": 0, "out": 0, "ms": 0, "n": 0, "err": 0, "traces": set(),
                                   "cost_ls": 0.0, "fb": [], "ttft": [], "tools": 0, "out_json": 0, "tool_in": 0,
                                   "out_list": [], "models": Counter(), "gpaths": Counter(),
                                   "model": rec["trace"].get("model", ""), "first": "", "last": ""})
        tr = rec["trace"]
        u = tr.get("usage", {})
        s["models"][tr.get("model", "")] += 1                                 # full model DISTRIBUTION, not just first
        s["gpaths"][tr.get("graph_path") or paths.get(str(rec.get("id")), "")] += 1   # trace field, else tree-derived
        s["out_list"].append(u.get("output_tokens", 0))                       # for the output-length DISTRIBUTION
        s["out_json"] += 1 if _is_json(tr.get("output")) else 0                # deterministic output-structure signal
        s["tool_in"] += 1 if any(m.get("role") == "tool" for m in tr.get("input_messages", []) or []) else 0
        s["in"] += u.get("input_tokens", 0)
        s["out"] += u.get("output_tokens", 0)
        s["ms"] += tr.get("runtime_ms", 0)
        s["n"] += 1
        s["err"] += 1 if tr.get("error") else 0
        s["cost_ls"] += tr.get("cost_ls") or 0                # LangSmith's own $ (authoritative when present)
        s["tools"] += len(tr.get("tools_called") or [])       # tool-calling behaviour (captured, now surfaced)
        if tr.get("feedback_score") is not None:
            s["fb"].append(tr["feedback_score"])              # quality (only when evals are attached)
        if tr.get("ttft_ms") is not None:
            s["ttft"].append(tr["ttft_ms"])                   # time-to-first-token (only when streamed)
        s["traces"].add(str(rec.get("trace_id") or ""))
        st = rec.get("start_time") or ""
        if st:
            s["first"] = min(s["first"] or st, st)
            s["last"] = max(s["last"], st)

    nodes = []
    for key, s in stats.items():
        n = max(1, s["n"])
        _mm = s["models"].most_common()
        nodes.append({"key": key, "node": s["node"], "variant": s["variant"], "untagged": s["untagged"],
                      "samples": s["n"], "traces": len(s["traces"]),
                      "graph_path": s["gpaths"].most_common(1)[0][0] if s["gpaths"] else "",  # subgraph nesting
                      "model": (_mm[0][0] if _mm else s["model"]),          # DOMINANT model (mode), not the first sample
                      "models": ", ".join("%s x%d" % (m or "?", c) for m, c in _mm),
                      "models_list": [{"name": m or "?", "calls": c} for m, c in _mm],   # distribution (app view)
                      "mixed_model": len(s["models"]) > 1,
                      "avg_in": round(s["in"] / n), "avg_out": round(s["out"] / n),
                      "avg_ms": round(s["ms"] / n), "errors": s["err"], "error_rate": round(s["err"] / n, 3),
                      "cost_ls": round(s["cost_ls"], 5), "avg_tools": round(s["tools"] / n, 1),
                      "feedback_score": (round(sum(s["fb"]) / len(s["fb"]), 3) if s["fb"] else None),
                      "ttft_ms": (round(sum(s["ttft"]) / len(s["ttft"])) if s["ttft"] else None),
                      "out_type": ("json" if s["out_json"] > n * 0.5 else            # deterministic output structure
                                   ("short" if round(s["out"] / n) < 25 else "free-form")),
                      "tool_input_rate": round(s["tool_in"] / n, 2),
                      "out_p50": _pct(s["out_list"], 0.50), "out_p95": _pct(s["out_list"], 0.95),  # output distribution
                      "first": s["first"], "last": s["last"]})

    # edges — observed node->node transitions across all traces (topology)
    edge_ct = {}
    for order in node_order_per_trace.values():
        for a, b in zip(order, order[1:]):
            edge_ct[(a, b)] = edge_ct.get((a, b), 0) + 1
    edges = [{"src": a, "dst": b, "count": c} for (a, b), c in sorted(edge_ct.items(), key=lambda kv: -kv[1])]

    return Graph(agent, buckets, nodes, edges, len([t for t in by_trace if t]))
