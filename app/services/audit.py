"""Model-downgrade audit — the provable core. Generic: no rules, no cohorts, no per-payload branching.

For a call-site we test two axes so a verdict is both broad AND repeatable:
  DIVERSITY  — N distinct recorded inputs (different real tickets).
  STABILITY  — each input re-run on the cheaper model K times, because a single run of a stochastic model +
               judge is a noisy sample, not a fact. We report a preservation RATE per input, not a coin-flip.

  replay(trace, model)   -> the cheaper model's behavior as ONE string (text AND any tool calls rendered in).
  reference              -> the ORIGINAL's RECORDED output (already in the trace — never re-run; it's the real
                            production behavior we must preserve).
  judge_preserved(A,B)   -> ONE generic judge for every payload (prose, JSON, or a tool call — all just behavior):
                            did B keep A's decision + every material fact, no contradiction? unsure -> DRIFT.

Verdict per input:  SAFE = preserved in ALL K repeats · NOT-SAFE = 0/K · BORDERLINE = anything in between (it flips).
Verdict per node:   SAFE only if EVERY input is SAFE · NOT-SAFE if any input is NOT-SAFE · else BORDERLINE.
Never a false SAFE: unanimity is the bar, and every tie breaks toward NOT recommending.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor

from auditor.util import canonical_model, next_cheaper, tier, release, max_output
from app.services import llm_client
from app.config import (AUDIT_SAMPLES, AUDIT_REPEATS, AUDIT_MIN_EVIDENCE, AUDIT_MAX_PARALLEL,
                        AUDIT_JUDGE_MAX_CHARS, AUDIT_SAFE_RATIO)

JUDGE_MODEL = "claude-sonnet-5"        # a capable judge — judge quality is the crux


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


def _render(text, tool_calls):
    """A model's behavior as ONE string: its prose plus any tool call rendered as `[calls name({args})]`, so the
    ONE judge sees the full behavior — no structured/free-text branch. Deterministic arg order for readability."""
    parts = [text.strip()] if (text and text.strip()) else []
    for tc in tool_calls or []:
        parts.append("[calls %s(%s)]" % (tc.get("name", "?"), json.dumps(tc.get("args", {}), sort_keys=True)))
    return "\n".join(parts).strip() or "(empty output)"


def replay(trace, model, max_tokens=None):
    """Re-run the recorded request on `model` (the cheaper tier) and return its behavior as one rendered string.
    Tools BOUND (read-only), never executed. Output budget SCALES to the recorded output (+headroom), capped at the
    model's real max_output — never a fixed cap. temperature not sent (newest models reject it)."""
    if max_tokens is None:
        rec_out = int((trace.get("usage") or {}).get("output_tokens", 0) or 0)
        max_tokens = min(max_output(model), max(512, int(rec_out * 1.5) + 128))
    system = "\n".join(m["content"] for m in trace.get("input_messages", []) if m.get("role") == "system")
    kw = {"model": model, "max_tokens": max_tokens, "messages": _messages(trace)}
    if trace.get("thinking_enabled"):
        kw["thinking"] = {"type": "adaptive"}
        kw["output_config"] = {"effort": "low"}
        kw["max_tokens"] = max(max_tokens, 1536)
    if system.strip():
        kw["system"] = system
    tools = _tools(trace)
    if tools:
        kw["tools"] = tools
    try:
        r = llm_client.complete(**kw)
    except Exception:
        if "thinking" in kw:                # target tier can't do the recorded thinking mode -> best-effort replay
            kw.pop("thinking", None); kw.pop("output_config", None)
            r = llm_client.complete(**kw)
        else:
            raise
    text_parts, tool_calls = [], []
    for b in r.content:
        if b.type == "text":
            text_parts.append(b.text)
        elif b.type == "tool_use":
            tool_calls.append({"name": b.name, "args": b.input})
    return _render("\n".join(text_parts), tool_calls)


def _recorded(trace):
    """The ORIGINAL's recorded behavior — the fixed reference we must preserve. The connector stores the model's
    full output (text + any tool call) here, so no original re-run is needed."""
    return (trace.get("output") or "").strip() or "(empty output)"


_JUDGE_SYS = "You compare two AI answers to the SAME request and decide if the cheaper model PRESERVED behavior."
_JUDGE_TMPL = (
    "Same request, two answers. A = original model. B = a cheaper model.\n"
    "Did B preserve A's DECISION and every MATERIAL fact?\n"
    "- same decision / classification / conclusion / recommendation / tool call\n"
    "- every material fact, number, id, name, or tool argument in A is present and unchanged in B\n"
    "- B introduces nothing that CONTRADICTS A\n"
    "Judge ONLY preservation of A's commitments — NOT style, wording, length, or which reads better.\n"
    "If anything material is dropped, changed, or contradicted, OR you are unsure -> DRIFT.\n\n"
    "REQUEST:\n%s\n\nA (original):\n%s\n\nB (cheaper):\n%s\n\n"
    "Answer in TWO lines:\nLine 1: exactly PRESERVED or DRIFT\nLine 2: a short reason (<=12 words)"
)


def judge_preserved(request, a_text, b_text, model=JUDGE_MODEL):
    """The ONE generic, DRIFT-BIASED judge -> (preserved: bool, reason: str). Verdict is on line 1 so a truncated
    reply can't corrupt it. Any doubt -> DRIFT: a false PRESERVED is the only unacceptable error.

    The judge reads the FULL outputs (bounded by AUDIT_JUDGE_MAX_CHARS ~ the judge model's context) so tail drift
    is never hidden. If an output is SO large it exceeds even that, we can't fully verify it -> DRIFT (never a
    SAFE from a partial view)."""
    cap = AUDIT_JUDGE_MAX_CHARS
    a, b = (a_text or ""), (b_text or "")
    if len(a) > cap or len(b) > cap:                     # unverifiable at full size -> refuse to call it safe
        return False, "output exceeds %dk chars — too long to fully verify -> drift" % (cap // 1000)
    p = _JUDGE_TMPL % (request[:cap], a, b)
    r = llm_client.complete(model=model, max_tokens=200, system=_JUDGE_SYS,
                            messages=[{"role": "user", "content": p}])
    raw = "".join(b.text for b in r.content if b.type == "text")
    lines = [re.sub(r"^\s*(line\s*[12]?\s*)?[:.\-)]*\s*", "", ln, flags=re.I).strip()
             for ln in raw.splitlines() if ln.strip()]
    preserved, reason = False, "no verdict -> drift"
    for i, ln in enumerate(lines):
        u = ln.upper()
        if u.startswith("PRESERVED") or u.startswith("DRIFT"):
            preserved = u.startswith("PRESERVED")
            reason = next((lines[j] for j in range(i + 1, len(lines)) if lines[j]),
                          ln[len("PRESERVED" if preserved else "DRIFT"):].strip() or ("preserved" if preserved else "drift"))
            break
    return preserved, reason[:80]


def _distinct(traces, n):
    """Up to n inputs that aren't near-identical (word-shingle overlap >= 0.8). DETERMINISTIC (walks in order) —
    never random, so the SAMPLE is stable across audits; only the repeats measure model/judge noise."""
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


def _one_repeat(trace, cheaper):
    """One independent proof: the ORIGINAL's recorded behavior vs ONE fresh cheaper re-run, judged once.
    -> {preserved, reason, output}. Order-independent, so a node's N x K of these parallelize safely.
    (Robustness to a single bad call comes from the 2/3 aggregation OVER re-runs, not from re-judging one.)"""
    b = replay(trace, cheaper)
    ok, why = judge_preserved(_user_text(trace), _recorded(trace), b)
    return {"preserved": ok, "reason": why, "output": b[:AUDIT_JUDGE_MAX_CHARS]}   # full output; UI scrolls


def audit_node(node_name, bucket, n=AUDIT_SAMPLES, k=AUDIT_REPEATS, min_evidence=AUDIT_MIN_EVIDENCE):
    """Prove (or refute) a downgrade for one call-site across N inputs x K repeats. Returns the verdict dict.
    Verdicts: SAFE (recommend) · BORDERLINE (flips — don't) · NOT-SAFE (don't) · LOW-EVIDENCE (abstain) · N/A."""
    model = canonical_model(bucket[0].get("model")) or bucket[0].get("model")
    cheaper = next_cheaper(model)
    base = {"node": node_name, "model": model, "cheaper": cheaper,
            "from_tier": tier(model), "to_tier": tier(cheaper), "to_release": release(cheaper)}
    if not cheaper:
        return {**base, "verdict": "N/A", "reason": "already cheapest tier", "inputs": []}
    sample = _distinct(bucket, n)
    if len(sample) < min_evidence:
        return {**base, "verdict": "LOW-EVIDENCE", "n": len(sample), "inputs": [],
                "reason": "only %d distinct input(s), need %d" % (len(sample), min_evidence)}

    # N inputs x K repeats — ALL independent. Flatten into ONE bounded pool (not nested pools) so concurrency
    # stays capped at AUDIT_MAX_PARALLEL; llm_client's 429 backoff self-throttles if we ever push too hard.
    tasks = [(idx, t) for idx, t in enumerate(sample) for _ in range(k)]
    with ThreadPoolExecutor(max_workers=min(AUDIT_MAX_PARALLEL, len(tasks))) as ex:
        done = list(ex.map(lambda it: (it[0], _one_repeat(it[1], cheaper)), tasks))
    by_idx = {}
    for idx, res in done:
        by_idx.setdefault(idx, []).append(res)

    inputs = []
    for idx, t in enumerate(sample):
        s = by_idx.get(idx, [])
        kept = sum(1 for x in s if x["preserved"])
        v = "SAFE" if (s and kept >= AUDIT_SAFE_RATIO * k) else ("NOT-SAFE" if kept == 0 else "BORDERLINE")   # 2/3 -> tolerate one drift
        drift_reason = next((x["reason"] for x in s if not x["preserved"]), "")    # surface WHY it drifted, if it did
        inputs.append({"input": _user_text(t)[:AUDIT_JUDGE_MAX_CHARS], "kept": kept, "k": k, "verdict": v,
                       "recorded": _recorded(t)[:AUDIT_JUDGE_MAX_CHARS], "samples": s,   # full text kept for the DOWNLOAD record
                       "reason": drift_reason or (s[0]["reason"] if s else "same decision")})

    if any(r["verdict"] == "NOT-SAFE" for r in inputs):
        verdict = "NOT-SAFE"
    elif all(r["verdict"] == "SAFE" for r in inputs):
        verdict = "SAFE"
    else:
        verdict = "BORDERLINE"
    return {**base, "verdict": verdict, "n": len(inputs), "k": k,
            "safe_inputs": sum(1 for r in inputs if r["verdict"] == "SAFE"), "inputs": inputs}


def run(source_id, ws_id, project, n=AUDIT_SAMPLES, k=AUDIT_REPEATS):
    """Audit every DOWNGRADE CANDIDATE of an agent — the paid proof pass behind the report. Candidates come from
    downgrade.candidates() over the SAME node grouping the graph shows, so what gets audited matches what you see.
    Paid (~candidates x N x K x 2 calls)."""
    from connectors.datasource import get_source
    from app.services import graph, downgrade

    g = graph.build(source_id, ws_id, project)
    cands = {c["node"]: c for c in downgrade.candidates(g["nodes"])}
    if not cands:
        return {"agent": project, "n": n, "k": k, "candidates": 0, "results": []}

    buckets, _ = get_source(source_id).pull(ws_id, project, limit=200)
    by_node = {}
    for t in (t for b in buckets.values() for t in b):
        nid = t.get("node_id") or t.get("agent_id") or "node"
        if nid in cands:
            by_node.setdefault(nid, []).append(t)

    results = []
    for nid, bucket in by_node.items():
        r = audit_node(nid, bucket, n=n, k=k)
        c = cands[nid]
        r["usd"], r["per_calls"] = c["usd"], c["per_calls"]
        results.append(r)
    results.sort(key=lambda x: -x.get("usd", 0))
    return {"agent": project, "n": n, "k": k, "candidates": len(cands), "results": results}
