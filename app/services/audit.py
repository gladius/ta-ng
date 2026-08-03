"""Model-downgrade audit — the provable core. Generic: no rules, no cohorts, no clustering.

For a call-site: pick N distinct recorded inputs, faithfully re-run each on the next-cheaper tier, and judge
whether the answer is preserved.

  faithful replay  — replay the recorded messages (tool results are already text) with tool schemas BOUND but
                     NEVER executed. Side-effect-free: one completion, no live tools, no graph re-run.
  judge            — one generic question, any payload kind: "does B keep the same decision + key facts as A?"
"""
import json
from concurrent.futures import ThreadPoolExecutor

from auditor.util import canonical_model, next_cheaper, tier, release
from app.services import llm_client


def _user_text(t):
    return " ".join(m["content"] for m in t.get("input_messages", []) if m.get("role") == "user")


def _messages(trace):
    """Recorded conversation as an Anthropic messages array: system pulled out, roles mapped, consecutive
    same-role merged (the API needs alternation), must start with user. Recorded tool results are text."""
    out = []
    for m in trace.get("input_messages", []):
        if m.get("role") == "system":
            continue
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + content
        else:
            out.append({"role": role, "content": content})
    if not out:
        out = [{"role": "user", "content": "(no user turn)"}]
    if out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(continue)"})
    return out


def _tools(trace):
    out = []
    for name, sch in trace.get("tools_defined", []) or []:
        try:
            d = json.loads(sch) if isinstance(sch, str) else (sch or {})
            out.append({"name": d.get("name", name), "description": (d.get("description") or "")[:1024],
                        "input_schema": d.get("input_schema") or {"type": "object", "properties": {}}})
        except Exception:
            continue
    return out


def replay(trace, model, max_tokens=700):
    """Faithful re-run of the recorded request on `model`. Tools BOUND (read-only), never executed. -> text."""
    system = "\n".join(m["content"] for m in trace.get("input_messages", []) if m.get("role") == "system")
    kw = {"model": model, "max_tokens": max_tokens, "messages": _messages(trace)}
    if system.strip():
        kw["system"] = system
    tools = _tools(trace)
    if tools:
        kw["tools"] = tools
    r = llm_client.complete(**kw)
    parts = []
    for b in r.content:
        if b.type == "text":
            parts.append(b.text)
        elif b.type == "tool_use":
            parts.append("[tool_call %s %s]" % (b.name, json.dumps(b.input, sort_keys=True)))
    return "\n".join(parts).strip()


def judge_preserved(request, recorded, candidate, model="claude-haiku-4-5"):
    """Generic preservation check — does B keep the DECISION + key facts of A? -> bool. Works for any payload."""
    p = ("Same request, two AI answers. Does answer B preserve the DECISION and the key facts of answer A "
         "(same classification/grade/conclusion; no dropped or changed facts, numbers, or ids)? "
         "Reply ONLY YES or NO.\n\nREQUEST:\n%s\n\nA (original):\n%s\n\nB (candidate):\n%s"
         % (request[:2000], (recorded or "")[:1000], (candidate or "")[:1000]))
    r = llm_client.complete(model=model, max_tokens=5, system="Reply only YES or NO.",
                            messages=[{"role": "user", "content": p}])
    return "yes" in "".join(b.text for b in r.content if b.type == "text").lower()


def _distinct(traces, n):
    """Up to n inputs that aren't near-identical (cheap word-shingle overlap, threshold 0.8)."""
    def sh(s):
        w = s.lower().split()
        return set(tuple(w[i:i + 3]) for i in range(max(0, len(w) - 2)))
    kept, kept_sh = [], []
    for t in traces:
        s = sh(_user_text(t))
        if any((len(s & k) / max(1, len(s | k))) >= 0.8 for k in kept_sh):
            continue
        kept.append(t); kept_sh.append(s)
        if len(kept) >= n:
            break
    return kept


def audit_node(node_name, bucket, n=5):
    """Prove (or refute) a downgrade for one call-site. Returns the verdict dict. Paid (N replays + N judges)."""
    model = canonical_model(bucket[0].get("model")) or bucket[0].get("model")
    cheaper = next_cheaper(model)
    if not cheaper:
        return {"node": node_name, "model": model, "verdict": "N/A", "reason": "already cheapest tier"}
    sample = _distinct(bucket, n)

    def _one(t):
        cand = replay(t, cheaper)                                    # re-run the cheaper tier
        ok = judge_preserved(_user_text(t), t.get("output", ""), cand)   # judge decision-preservation
        return {"preserved": ok, "recorded": (t.get("output") or "")[:80], "candidate": cand[:80]}

    if sample:                                                       # the N inputs are independent → run concurrent
        with ThreadPoolExecutor(max_workers=min(4, len(sample))) as ex:
            samples = list(ex.map(_one, sample))
    else:
        samples = []
    kept = sum(1 for s in samples if s["preserved"])
    verdict = "PRESERVED" if samples and kept == len(samples) else ("DRIFT" if samples else "ABSTAIN")
    return {"node": node_name, "model": model, "cheaper": cheaper, "n": len(samples),
            "preserved": kept, "verdict": verdict, "samples": samples,
            "from_tier": tier(model), "to_tier": tier(cheaper),                # capability drop, for the UI label
            "to_release": release(cheaper)}


def run(source_id, ws_id, project, n=3):
    """Audit every DOWNGRADE CANDIDATE of an agent automatically — the one call the graph page's Downgrade
    tab triggers. Paid (~candidates x n x 2 LLM calls).

    Candidates come from downgrade.candidates() on the SAME node_id grouping the graph shows, so what gets
    audited exactly matches what you see. Traces are re-grouped by node_id (the framework-agnostic key), never
    by the connector's variant bucket keys — so the join can't silently miss. Not random nodes: a random node
    may already be cheapest or be a router, wasting calls; candidates are the nodes a downgrade can even apply to.
    """
    from connectors.datasource import get_source
    from app.services import graph, downgrade

    g = graph.build(source_id, ws_id, project)
    cands = {c["node"]: c for c in downgrade.candidates(g["nodes"])}      # node_id -> {model, cheaper, usd, ...}
    if not cands:
        return {"agent": project, "n": n, "candidates": 0, "results": []}

    buckets, _ = get_source(source_id).pull(ws_id, project, limit=200)
    by_node = {}
    for t in (t for b in buckets.values() for t in b):
        nid = t.get("node_id") or t.get("agent_id") or "node"
        if nid in cands:
            by_node.setdefault(nid, []).append(t)

    results = []
    for nid, bucket in by_node.items():
        r = audit_node(nid, bucket, n=n)
        c = cands[nid]
        r["usd"], r["per_calls"] = c["usd"], c["per_calls"]              # carry the $ estimate onto the verdict
        results.append(r)
    results.sort(key=lambda x: -x.get("usd", 0))
    return {"agent": project, "n": n, "candidates": len(cands), "results": results}
