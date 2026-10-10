"""Execution graph: folds one agent's run trees into one graph and derives the audited call-sites from structure.

A ReAct node's reason call and post-tool call are different prompts, so they become different call-sites. An untagged
call takes its node from the nearest named parent chain; run.name would lump every step into one "ChatAnthropic" bucket.
"""

import re
from collections import Counter, OrderedDict

from contract import eligible

# Chain-run names that are scaffolding, not node identities.
_GENERIC = {"", "langgraph", "runnablesequence", "runnableparallel", "runnablelambda",
            "runnableassign", "__start__", "__end__", "channelwrite", "_write", "_route"}


def _is_generic(name):
    return str(name or "").strip().lower() in _GENERIC


def _pct(values, q):
    """The q-quantile of a list (nearest rank), 0 if empty."""
    if not values:
        return 0
    xs = sorted(values)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _is_json(text):
    """Does the recorded output parse as JSON (fence-tolerant)?"""
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
    """The prompt's role sequence, so pre-tool and post-tool calls at one node don't merge."""
    return tuple(m["role"] for m in trace.get("input_messages", []))


def _frame_variants(items, min_prefix=6, min_frac=0.2, min_cooccur=0.5):
    """Split a node's no-tool calls into call-sites only for distinct authored call types ('Plan ...' vs
    'Critique ...'), so a map/fan-out node is never fragmented. A frame counts only if its long shared prefix recurs
    in >= min_frac of calls, and >= 2 such frames must co-occur within one trace.
    items = [(id, trace_id, user_text)]; returns {id: frame_slug or ''}."""
    n = len(items)
    if n < 2:
        return {i: "" for i, _, _ in items}
    # Sorting puts same-frame calls side by side; the running word prefix shrinks to their shared frame.
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
    real = {c["idx"] for c in clusters if len(c["members"]) >= max(2, min_frac * n) and len(c["prefix"]) >= min_prefix}
    if len(real) < 2:
        return {i: "" for i, _, _ in items}
    per_trace, calls_per_trace = {}, {}
    for cid, tid, _ in items:
        calls_per_trace[tid] = calls_per_trace.get(tid, 0) + 1
        if cluster_of[cid] in real:
            per_trace.setdefault(tid, set()).add(cluster_of[cid])
    multi_call = [t for t, c in calls_per_trace.items() if c >= 2]
    cooccur = [t for t in multi_call if len(per_trace.get(t, ())) >= 2]
    if not multi_call or len(cooccur) < min_cooccur * len(multi_call):
        return {i: "" for i, _, _ in items}
    slug = {}
    for c in clusters:
        if c["idx"] in real:
            slug[c["idx"]] = re.sub(r"[^a-z0-9]+", "", "".join(x.lower() for x in c["prefix"][:3]))[:14] or ("f%d" % c["idx"])
    return {cid: slug.get(cluster_of[cid], "") for cid, _, _ in items}


def _resolve_node(rec, by_id):
    """(node_label, tagged) for an llm run: metadata node, else nearest named parent chain, else run.name
    with tagged=False so the UI can warn."""
    if rec.get("node_hint"):
        return str(rec["node_hint"]), True
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
    return str(rec.get("name") or "llm"), False


def _resolve_path(rec, by_id, exclude=None):
    """Named ancestor nodes of a run, outer -> inner ('' at top level); the fallback when a trace has no graph_path.
    `exclude` drops the run's own container chain, which shares its name (else 'agent/supervisor/supervisor')."""
    path, seen, cur = [], set(), rec.get("parent_run_id")
    while cur and str(cur) not in seen:
        seen.add(str(cur))
        p = by_id.get(str(cur))
        if p is None:
            break
        name = p.get("node_hint") or (p.get("name") if p.get("run_type") == "chain" else None)
        if name and not _is_generic(name) and str(name) != str(exclude):
            path.append(str(name))
        cur = p.get("parent_run_id")
    return "/".join(reversed(path))


class Graph:
    def __init__(self, agent, buckets, nodes, edges, trace_count, errors_excluded=0, revisions=None,
                 structural_nodes=None, run_totals=None, node_run_types=None):
        self.agent = agent
        self.buckets = buckets                           # OrderedDict[key -> list[trace]]
        self.nodes = nodes
        self.edges = edges
        self.trace_count = trace_count
        self.errors_excluded = errors_excluded           # failed samples kept out of every bucket
        self.revisions = revisions or []
        self.structural_nodes = structural_nodes or []   # tool, retriever and function nodes
        self.run_totals = run_totals or {}               # run_type -> count over all fetched runs
        self.node_run_types = node_run_types or {}        # langgraph_node -> {run_type: count}


def build_graph(records, agent_default="agent"):
    """Fold run-records into one agent graph. Pure + deterministic; no I/O, no LLM."""
    records = [r for r in records if r]
    agent = next((r["agent_hint"] for r in records if r.get("agent_hint")), None) or agent_default

    by_trace = OrderedDict()
    for r in records:
        by_trace.setdefault(str(r.get("trace_id") or ""), []).append(r)

    # Pass 1: resolve each profileable llm sample to its node, shape and subgraph path.
    resolved = []                                        # (rec, node_label, tagged, sig)
    paths = {}
    for tid, runs in by_trace.items():
        by_id = {str(r["id"]): r for r in runs if r.get("id")}
        for rec in sorted(runs, key=lambda r: (r.get("dotted_order") or "", r.get("start_time") or "")):
            if rec.get("trace") is not None and not rec.get("skip"):
                nl, tagged = _resolve_node(rec, by_id)
                paths[str(rec.get("id"))] = _resolve_path(rec, by_id, exclude=nl)
                resolved.append((rec, nl, tagged, _shape_sig(rec["trace"])))

    no_tool_items = {}
    for rec, nl, tagged, sig in resolved:
        if "tool" not in sig:
            ut = " ".join(m.get("content", "") for m in rec["trace"].get("input_messages", []) if m.get("role") == "user")
            no_tool_items.setdefault(nl, []).append((rec.get("id"), rec.get("trace_id") or "", ut))
    frame_label = {}
    for nl, items in no_tool_items.items():
        frame_label.update(_frame_variants(items))

    def _full_variant(rec, sig):
        """'after-tool' for a ReAct post-tool call, else the frame slug, else 'initial'."""
        if "tool" in sig:
            return "after-tool"
        return frame_label.get(rec.get("id"), "") or "initial"

    def _gp(rec):
        """A sample's subgraph path (its graph_path, else tree-derived). Part of the call-site key, so 'billing/worker'
        and 'refund/worker' never merge."""
        return (rec["trace"].get("graph_path") or paths.get(str(rec.get("id")), "")).strip("/")

    variants_per_node = {}                               # per (path, node): one subgraph's variants never split another
    for rec, nl, tagged, sig in resolved:
        variants_per_node.setdefault((_gp(rec), nl), set()).add(_full_variant(rec, sig))

    # Pass 2: key 'agent/[path/]node[~variant]', with a variant only where a call-site has several. A failed sample
    # (contract.eligible) counts against its call-site but stays out of the audited bucket and stats.
    buckets = OrderedDict()
    stats = OrderedDict()
    excluded = OrderedDict()
    revs = set()
    for rec, nl, tagged, sig in resolved:
        gp = _gp(rec)
        multi = len(variants_per_node.get((gp, nl), ())) > 1
        variant = _full_variant(rec, sig) if multi else ""
        key = "%s/%s%s%s" % (agent, (gp + "/") if gp else "", nl, ("~" + variant) if variant else "")
        if not eligible(rec["trace"])[0]:
            excluded[key] = excluded.get(key, 0) + 1
            continue
        if rec["trace"].get("revision"):
            revs.add(rec["trace"]["revision"])
        buckets.setdefault(key, []).append(rec["trace"])
        s = stats.setdefault(key, {"node": nl, "variant": variant, "untagged": not tagged,
                                   "in": 0, "out": 0, "ms": 0, "n": 0, "traces": set(),
                                   "cost_ls": 0.0, "fb": [], "ttft": [], "tools": 0, "out_json": 0, "tool_in": 0,
                                   "out_list": [], "models": Counter(), "gpaths": Counter(),
                                   "model": rec["trace"].get("model", ""), "first": "", "last": ""})
        tr = rec["trace"]
        u = tr.get("usage", {})
        s["models"][tr.get("model", "")] += 1
        s["gpaths"][_gp(rec)] += 1
        s["out_list"].append(u.get("output_tokens", 0))
        s["out_json"] += 1 if _is_json(tr.get("output")) else 0
        s["tool_in"] += 1 if any(m.get("role") == "tool" for m in tr.get("input_messages", []) or []) else 0
        s["in"] += u.get("input_tokens", 0)
        s["out"] += u.get("output_tokens", 0)
        s["ms"] += tr.get("runtime_ms", 0)
        s["n"] += 1
        s["cost_ls"] += tr.get("cost_ls") or 0                # LangSmith's own $ cost, authoritative when present
        s["tools"] += len(tr.get("tools_called") or [])
        if tr.get("feedback_score") is not None:
            s["fb"].append(tr["feedback_score"])
        if tr.get("ttft_ms") is not None:
            s["ttft"].append(tr["ttft_ms"])
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
                      "graph_path": s["gpaths"].most_common(1)[0][0] if s["gpaths"] else "",
                      "model": (_mm[0][0] if _mm else s["model"]),          # most common model, not the first sample
                      "models": ", ".join("%s x%d" % (m or "?", c) for m, c in _mm),
                      "models_list": [{"name": m or "?", "calls": c} for m, c in _mm],
                      "mixed_model": len(s["models"]) > 1,
                      "avg_in": round(s["in"] / n), "avg_out": round(s["out"] / n),
                      "avg_ms": round(s["ms"] / n), "errors_excluded": excluded.get(key, 0),
                      "error_rate": round(excluded.get(key, 0) / (n + excluded.get(key, 0)), 3),
                      "cost_ls": round(s["cost_ls"], 5), "avg_tools": round(s["tools"] / n, 1),
                      "feedback_score": (round(sum(s["fb"]) / len(s["fb"]), 3) if s["fb"] else None),
                      "ttft_ms": (round(sum(s["ttft"]) / len(s["ttft"])) if s["ttft"] else None),
                      "out_type": ("json" if s["out_json"] > n * 0.5 else
                                   ("short" if round(s["out"] / n) < 25 else "free-form")),
                      "tool_input_rate": round(s["tool_in"] / n, 2),
                      "out_p50": _pct(s["out_list"], 0.50), "out_p95": _pct(s["out_list"], 0.95),
                      "first": s["first"], "last": s["last"]})

    # Deployment topology over all run types; keys match the llm nodes' (minus ~variant) so edges join to nodes.
    def _skey(gp, label):
        gp = (gp or "").strip("/")
        return "%s/%s%s" % (agent, (gp + "/") if gp else "", label)

    def _run_key(rec, by_id):
        """Topology key for an llm node, named chain, tool or retriever run, else None (a failed llm is no edge).
        A container chain and its llm share one key."""
        rt = rec.get("run_type")
        if rt in (None, "llm") and rec.get("trace") is not None and not rec.get("skip"):
            if not eligible(rec["trace"])[0]:
                return None
            nl, _ = _resolve_node(rec, by_id)
            return _skey(_gp(rec), nl)
        name = rec.get("node_hint") or rec.get("name")
        if not name or _is_generic(name):
            return None
        if rt in ("chain", "tool", "retriever"):
            return _skey((rec.get("graph_path") or "").strip("/"), name)
        return None

    edge_ct = {}
    for tid, runs in by_trace.items():
        by_id = {str(r["id"]): r for r in runs if r.get("id")}
        seq = []
        for rec in sorted(runs, key=lambda r: (r.get("dotted_order") or "", r.get("start_time") or "")):
            k = _run_key(rec, by_id)
            if k and (not seq or seq[-1] != k):
                seq.append(k)
        for a, b in zip(seq, seq[1:]):                    # A->B->A cycles (ReAct) are real
            edge_ct[(a, b)] = edge_ct.get((a, b), 0) + 1
    edges = [{"src": a, "dst": b, "count": c} for (a, b), c in sorted(edge_ct.items(), key=lambda kv: -kv[1])]

    # Tool and retriever nodes: calls, errors and latency only, no tokens or cost.
    struct = OrderedDict()
    for tid, runs in by_trace.items():
        for rec in runs:
            if rec.get("run_type") not in ("tool", "retriever"):
                continue
            name = rec.get("node_hint") or rec.get("name") or rec.get("run_type")
            if _is_generic(name):
                continue
            gp = (rec.get("graph_path") or "").strip("/")
            a = struct.setdefault(_skey(gp, name),
                                  {"key": _skey(gp, name), "type": rec["run_type"], "node": name,
                                   "graph_path": gp, "calls": 0, "errors": 0, "ms": 0, "traces": set()})
            a["calls"] += 1
            a["errors"] += 1 if rec.get("error") else 0
            a["ms"] += rec.get("runtime_ms") or 0
            a["traces"].add(str(rec.get("trace_id") or ""))
    structural_nodes = [{"key": a["key"], "type": a["type"], "node": a["node"], "graph_path": a["graph_path"],
                         "calls": a["calls"], "errors": a["errors"], "traces": len(a["traces"]),
                         "error_rate": round(a["errors"] / max(1, a["calls"]), 3),
                         "avg_ms": round(a["ms"] / max(1, a["calls"]))}
                        for a in struct.values()]

    # Function nodes: a named langgraph_node with no llm, tool or retriever runs (pure code, traced as a chain).
    run_totals = Counter()
    node_rt = {}
    for tid, runs in by_trace.items():
        for rec in runs:
            run_totals[rec.get("run_type")] += 1
            node_rt.setdefault(rec.get("node_hint") or "(no node)", Counter())[rec.get("run_type")] += 1
    fn = OrderedDict()
    for tid, runs in by_trace.items():
        for rec in runs:
            if rec.get("run_type") != "chain":
                continue
            nl = rec.get("node_hint")
            if not nl or _is_generic(nl):
                continue
            c = node_rt.get(nl) or {}
            if c.get("llm") or c.get(None) or c.get("tool") or c.get("retriever"):
                continue
            gp = (rec.get("graph_path") or "").strip("/")
            a = fn.setdefault(_skey(gp, nl), {"key": _skey(gp, nl), "type": "function", "node": nl,
                                              "graph_path": gp, "calls": 0, "errors": 0, "ms": 0,
                                              "traces": set(), "out_sample": ""})
            a["calls"] += 1
            a["errors"] += 1 if rec.get("error") else 0
            a["ms"] += rec.get("runtime_ms") or 0
            a["traces"].add(str(rec.get("trace_id") or ""))
            if not a["out_sample"] and rec.get("out_sample"):
                a["out_sample"] = rec["out_sample"]
    structural_nodes += [{"key": a["key"], "type": a["type"], "node": a["node"], "graph_path": a["graph_path"],
                          "calls": a["calls"], "errors": a["errors"], "traces": len(a["traces"]),
                          "error_rate": round(a["errors"] / max(1, a["calls"]), 3),
                          "avg_ms": round(a["ms"] / max(1, a["calls"])), "out_sample": a["out_sample"]}
                         for a in fn.values()]

    return Graph(agent, buckets, nodes, edges, len([t for t in by_trace if t]),
                 errors_excluded=sum(excluded.values()), revisions=sorted(revs),
                 structural_nodes=structural_nodes,
                 run_totals=dict(run_totals), node_run_types={k: dict(v) for k, v in node_rt.items()})
