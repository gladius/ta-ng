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

Verdict per input:  cheaper PERFECT (K/K) -> SAFE (can't do better). Otherwise we can't tell downgrade-drift from
                    node-noise, so we measure the ORIGINAL's OWN consistency (self-variance) on that same input and
                    judge the cheaper RELATIVE to it: as steady as the original -> SAFE · clearly worse -> NOT-SAFE ·
                    one repeat worse, OR the original can't even reproduce itself (unreliable anchor) -> BORDERLINE.
Verdict per node:   SAFE only if EVERY input is SAFE · NOT-SAFE if any input is NOT-SAFE · else BORDERLINE.
Never a false SAFE: ties break toward NOT recommending; an unverifiable anchor is BORDERLINE, never SAFE.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor

from auditor.util import canonical_model, next_cheaper, tier, release, max_output
from app.services import llm_client
from app.config import (AUDIT_SAMPLES, AUDIT_REPEATS, AUDIT_MIN_EVIDENCE, AUDIT_MAX_PARALLEL,
                        AUDIT_JUDGE_MAX_CHARS, AUDIT_SAFE_RATIO, AUDIT_SELF_BASELINE, AUDIT_SELF_FLOOR)

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
    """The recorded BOUND tools as NEUTRAL tool intents [{name, description, schema}] for llm_client.run. The stored
    schema may be Anthropic-style (`input_schema`) OR OpenAI/LangChain function-style (`parameters`) — read BOTH,
    else an OpenAI-format tool binds with an EMPTY schema and the cheaper model can't reproduce the recorded call
    (a false 'wrong tool/args' drift). Provider translation lives in llm_client, not here."""
    out = []
    for name, sch in trace.get("tools_defined", []) or []:
        try:
            d = json.loads(sch) if isinstance(sch, str) else (sch or {})
            schema = d.get("input_schema") or d.get("parameters") or {"type": "object", "properties": {}}
            out.append({"name": d.get("name", name), "description": (d.get("description") or "")[:1024],
                        "schema": schema})
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


def _tool_choice(raw):
    """Convert a recorded tool_choice to NEUTRAL form so a FORCED tool call is reproduced on replay (llm_client
    translates neutral -> provider). Returns None for auto / none / unset (let the model choose, matching the
    original), 'any' to force some tool, or {'tool': name} to force a specific one. Handles OpenAI ('required',
    {'type':'function','function':{'name':X}}), Anthropic ({'type':'any'|'tool'|'auto'}), and the bare tool-name
    string some frameworks log."""
    if not raw:
        return None
    if isinstance(raw, str):
        s = raw.lower()
        if s in ("required", "any"):
            return "any"
        if s in ("auto", "none", ""):
            return None
        return {"tool": raw}                              # a bare tool name -> force that tool
    if isinstance(raw, dict):
        t = (raw.get("type") or "").lower()
        if t in ("any", "required"):
            return "any"
        if t == "tool" and raw.get("name"):
            return {"tool": raw["name"]}
        if t == "function":                               # OpenAI {'type':'function','function':{'name':X}}
            name = (raw.get("function") or {}).get("name")
            return {"tool": name} if name else None
    return None                                           # auto / none / unknown -> don't force


def replay(trace, model, max_tokens=None):
    """Re-run the recorded request on `model` (the cheaper tier) and return its behavior as one rendered string.
    This expresses NEUTRAL INTENT only — bind these tools, force this tool_choice, use reasoning if the node did —
    and hands it to llm_client.run, which owns the provider request shape (swap that one boundary for litellm in
    prod). Tools BOUND (read-only), never executed. Output budget SCALES to the recorded output (+headroom), capped
    at the model's real max_output. temperature not sent (newest models reject it)."""
    if max_tokens is None:
        rec_out = int((trace.get("usage") or {}).get("output_tokens", 0) or 0)
        max_tokens = min(max_output(model), max(512, int(rec_out * 1.5) + 128))
    system = "\n".join(m["content"] for m in trace.get("input_messages", []) if m.get("role") == "system")
    tools = _tools(trace)
    res = llm_client.run(model=model, messages=_messages(trace), max_tokens=max_tokens,
                         system=(system.strip() or None), tools=tools,
                         tool_choice=(_tool_choice(trace.get("tool_choice")) if tools else None),
                         reasoning=bool(trace.get("thinking_enabled")))
    return _render(res["text"], res["tool_calls"])


def _recorded(trace):
    """The ORIGINAL's recorded behavior — the fixed reference we must preserve. The connector stores the model's
    full output (text + any tool call) here, so no original re-run is needed."""
    return (trace.get("output") or "").strip() or "(empty output)"


_JUDGE_SYS = ("You compare two AI answers to the SAME request and decide whether the CANDIDATE answer preserved "
              "the behavior of the REFERENCE answer.")
_JUDGE_TMPL = (
    "Same request, two answers. A = the REFERENCE answer. B = the CANDIDATE answer.\n"
    "(Neither is labelled better — judge only whether B keeps A's commitments.)\n"
    "Did B preserve A's DECISION and every MATERIAL fact?\n"
    "- same decision / classification / conclusion / recommendation / tool call\n"
    "- every material fact, number, id, name, or tool argument in A is present and unchanged in B\n"
    "- B introduces nothing that CONTRADICTS A\n"
    "- B ADDS no material claim, fact, commitment, or number that is NOT supported by A\n"
    "Judge ONLY preservation of A's commitments — NOT style, wording, length, or which reads better.\n"
    "If anything material is dropped, changed, contradicted, or added unsupported, OR you are unsure -> DRIFT.\n\n"
    "REQUEST:\n%s\n\nA (reference):\n%s\n\nB (candidate):\n%s\n\n"
    "Think in ONE short line (a <=15-word reason), THEN on the FINAL line output exactly PRESERVED or DRIFT."
)


def judge_preserved(request, a_text, b_text, model=JUDGE_MODEL):
    """The ONE generic, DRIFT-BIASED judge -> (preserved: bool, reason: str). Verdict is on line 1 so a truncated
    reply can't corrupt it. Any doubt -> DRIFT: a false PRESERVED is the only unacceptable error.

    The judge reasons in one line FIRST and commits the verdict on the LAST line (reasoning-before-verdict is more
    accurate than deciding cold). We read the LAST PRESERVED/DRIFT token anywhere in the reply, so a preamble like
    "Verdict: PRESERVED" still parses. The judge reads the FULL outputs (bounded by AUDIT_JUDGE_MAX_CHARS ~ the
    judge model's context) so tail drift is never hidden. If an output exceeds even that, we can't fully verify it,
    and if no verdict token is emitted at all -> DRIFT (never a SAFE from a partial or unparseable view)."""
    cap = AUDIT_JUDGE_MAX_CHARS
    a, b = (a_text or ""), (b_text or "")
    if len(a) > cap or len(b) > cap:                     # unverifiable at full size -> refuse to call it safe
        return False, "output exceeds %dk chars — too long to fully verify -> drift" % (cap // 1000)
    p = _JUDGE_TMPL % (request[:cap], a, b)
    r = llm_client.complete(model=model, max_tokens=200, system=_JUDGE_SYS,
                            messages=[{"role": "user", "content": p}])
    raw = "".join(b.text for b in r.content if b.type == "text")
    hits = list(re.finditer(r"\b(PRESERVED|DRIFT)\b", raw, flags=re.I))
    if not hits:
        return False, "no verdict -> drift"              # unparseable -> DRIFT (never a false SAFE)
    preserved = hits[-1].group(1).upper() == "PRESERVED"     # LAST token = the model's final commitment
    reason = next((ln.strip() for ln in raw.splitlines()     # reason-first: first line that isn't just the verdict
                   if ln.strip() and not re.fullmatch(r"(PRESERVED|DRIFT)[\s:.\-]*", ln.strip(), flags=re.I)),
                  "preserved" if preserved else "drift")
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


def _verdict(cheap_kept, self_kept, k):
    """Per-input verdict + a short note. If the cheaper model was PERFECT (K/K) -> SAFE (it can't do better; how
    noisy the original is doesn't matter). Otherwise judge the cheaper RELATIVE to the ORIGINAL's own self-variance
    on this input — self_kept out of k, where the recorded output is the free +1 anchor:
      - self below AUDIT_SELF_FLOOR   -> original can't reliably reproduce itself -> unreliable anchor -> BORDERLINE
      - cheaper as steady as original -> SAFE       (the drift was the model's own noise, not the downgrade)
      - one repeat worse than original-> BORDERLINE  (just under the noise floor — human check)
      - >= two worse than original    -> NOT-SAFE    (clearly worse than the model's own noise)
    self_kept is None when the baseline is off or k==1 -> fall back to the flat AUDIT_SAFE_RATIO rule."""
    if cheap_kept >= k:
        return "SAFE", ""
    if self_kept is None:                                   # baseline unavailable -> flat ratio fallback
        if cheap_kept == 0:
            return "NOT-SAFE", ""
        return ("SAFE", "") if cheap_kept >= AUDIT_SAFE_RATIO * k else ("BORDERLINE", "flips run-to-run")
    if self_kept < AUDIT_SELF_FLOOR * k:
        return "BORDERLINE", "original itself varies run-to-run — recorded output isn't a reliable reference"
    if cheap_kept >= self_kept:
        return "SAFE", "cheaper is as consistent as the original's own re-runs"
    if self_kept - cheap_kept == 1:
        return "BORDERLINE", "cheaper drifted one more time than the original does"
    return "NOT-SAFE", "cheaper consistently changed the decision vs the original's own noise"


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

    # PASS 1 — cheaper model, N inputs x K repeats, ALL independent. Flatten into ONE bounded pool (not nested
    # pools) so concurrency stays capped at AUDIT_MAX_PARALLEL; llm_client's 429 backoff self-throttles.
    tasks = [(idx, t) for idx, t in enumerate(sample) for _ in range(k)]
    with ThreadPoolExecutor(max_workers=min(AUDIT_MAX_PARALLEL, len(tasks))) as ex:
        done = list(ex.map(lambda it: (it[0], _one_repeat(it[1], cheaper)), tasks))
    by_idx = {}
    for idx, res in done:
        by_idx.setdefault(idx, []).append(res)
    cheap_kept = {idx: sum(1 for x in by_idx.get(idx, []) if x["preserved"]) for idx in range(len(sample))}

    # PASS 2 — self-variance baseline, ONLY on DOUBTFUL inputs (cheaper wasn't perfect). Re-runs the ORIGINAL model
    # to measure its OWN run-to-run consistency, so we can judge the cheaper RELATIVE to that noise floor instead of
    # a flat bar. The recorded output is the original's free sample #1 (the anchor, trivially preserved), so we add
    # only k-1 fresh original re-runs and count recorded as the +1 -> self_kept out of k, comparable to cheap_kept.
    self_kept, base_runs = {}, {}
    if AUDIT_SELF_BASELINE and k > 1:
        doubtful = [idx for idx in range(len(sample)) if cheap_kept[idx] < k]
        btasks = [(idx, sample[idx]) for idx in doubtful for _ in range(k - 1)]
        if btasks:
            with ThreadPoolExecutor(max_workers=min(AUDIT_MAX_PARALLEL, len(btasks))) as ex:
                bdone = list(ex.map(lambda it: (it[0], _one_repeat(it[1], model)), btasks))
            for idx, res in bdone:
                base_runs.setdefault(idx, []).append(res)
            for idx in doubtful:
                self_kept[idx] = 1 + sum(1 for x in base_runs.get(idx, []) if x["preserved"])   # +1 = recorded anchor

    inputs = []
    for idx, t in enumerate(sample):
        s = by_idx.get(idx, [])
        ck = cheap_kept[idx]
        sk = self_kept.get(idx)                       # None unless this input was doubtful and the baseline ran
        v, note = _verdict(ck, sk, k)
        drift_reason = next((x["reason"] for x in s if not x["preserved"]), "")    # surface WHY it drifted, if it did
        inputs.append({"input": _user_text(t)[:AUDIT_JUDGE_MAX_CHARS], "kept": ck, "k": k, "self_kept": sk,
                       "verdict": v, "note": note, "recorded": _recorded(t)[:AUDIT_JUDGE_MAX_CHARS],
                       "samples": s,   # cheaper re-runs — full text kept for the DOWNLOAD record
                       "baseline": base_runs.get(idx, []),   # original re-runs (self-variance), doubtful inputs only
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
    Paid (~candidates x N x K x 2 calls, plus up to (k-1) x 2 more per DOUBTFUL input for the self-variance baseline)."""
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
