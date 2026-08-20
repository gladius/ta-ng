"""Downgrade audit — shared replay/judge machinery + the (cache/compress) proof engine.

The DOWNGRADE verdict lives in app/services/downgrade_refset.py (prove_transform_refset): per input the ORIGINAL
model's own 5 outputs (recorded + re-runs) ARE the acceptable-behaviour reference set, and each cheaper re-run is
majority-voted to belong to it. `audit_node` samples N distinct inputs and delegates to it.

This module provides the pieces that path shares:
  replay(trace, model)   -> re-run the recorded request on `model`, behaviour as ONE string (text + tool calls).
  _recorded(trace)       -> the ORIGINAL's recorded output — the reference we must preserve (never re-run).
  _distinct / _tools / _tool_choice / _render / _messages -> input sampling + provider-neutral request shaping.

It ALSO keeps `prove_transform` + `judge_preserved` + `_verdict` — the self-variance proof engine used by the
CACHE and COMPRESS levers (currently disabled via LEVERS). The downgrade path no longer uses them.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor

from auditor.util import canonical_model, next_cheaper, tier, release, max_output
from app.services import llm_client
from app.config import (AUDIT_SAMPLES, AUDIT_REPEATS, AUDIT_MIN_EVIDENCE, AUDIT_MAX_PARALLEL,
                        AUDIT_JUDGE_MAX_CHARS, AUDIT_SAFE_RATIO, AUDIT_SELF_BASELINE, AUDIT_SELF_FLOOR,
                        AUDIT_DOWNGRADE_K,   # cheaper re-run count for the reference-set downgrade engine
                        JUDGE_MODEL)   # judge model role lives in config (.env-overridable), not hardcoded here


def _user_text(t):
    return " ".join(m["content"] for m in t.get("input_messages", []) if m.get("role") == "user")


def _input_text(t):
    """The FULL input a call sent — every message (system + user + prior turns / tool results). The diversity key for
    _distinct: variation is caught wherever it lives (query / tool result / system), and the shared fixed part dilutes
    into the overlap instead of faking diversity."""
    return "\n".join((m.get("content") or "") for m in t.get("input_messages", []))


def _system_text(t):
    return "\n".join((m.get("content") or "") for m in t.get("input_messages", []) if m.get("role") == "system")


_INSPECT_CHARS = 8000    # cap for the report's before/after INSPECTOR fields (full text still ships in the ZIP)


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
    at the model's real max_output. temperature left at the model DEFAULT on purpose — re-runs must SAMPLE the model's
    natural run-to-run variance (that's exactly what coherence/self-consistency measures), and the thinking path
    rejects a custom temperature anyway. The deterministic side (judges, optimizer, profiler) goes through complete()."""
    if max_tokens is None:
        # FAITHFUL budget: give the re-run the SAME room the ORIGINAL ran under (trace's max_output_tokens). A thinking
        # model (Gemini 2.5 thinks by default) counts thinking tokens AGAINST this budget, so a budget sized to the
        # visible output alone truncates/empties the re-run — a chopped output then looks "broke" to the judge. Applies
        # to BOTH the original re-runs and the cheaper re-runs so they're judged on equal footing.
        rec_max = int(trace.get("max_output_tokens") or 0)
        if rec_max > 0:
            max_tokens = min(max_output(model), rec_max)
        else:                                             # not recorded -> 3x the original's OUTPUT as headroom
            rec_out = int((trace.get("usage") or {}).get("output_tokens", 0) or 0)
            if rec_out <= 0:
                rec_out = len(trace.get("output") or "") // 4
            max_tokens = min(max_output(model), max(512, rec_out * 3))
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

    ⚠️ TECH DEBT — this is the OLD single-recorded-vs-rerun path still used by the cache + compress levers. It should
    be migrated to the SAME flow + config as model-tier downgrade (downgrade_refset: reference-set voting, the labeled
    single-line KEPT/BROKE parse, and AUDIT_JUDGE_MAX_TOKENS) instead of this outdated judge with its hardcoded
    max_tokens=200 (which a thinking model / injected reasoning would starve to an empty reply, exactly like the
    downgrade judge did). Left as-is for now; pick up when cache/compress are revisited.

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
    """Up to n inputs that differ in their PER-CALL VARIABLE content — the part that actually changes behaviour.
    DETERMINISTIC (walks the bucket in order), so the SAMPLE is stable across audits; only the repeats measure noise.

    We key diversity on what VARIES per call, NOT the whole prompt. A call-site is a shared static skeleton (the system
    config / instructions, byte-identical every call) + per-call content (the query, a tool result, an injected id).
    The static carries ZERO behavioural variety yet can dwarf the prompt by bytes, so whole-prompt similarity goes
    BLIND to real variety under a big fixed prefix (8 genuinely different tickets under an 8k-token system read as 1 —
    letting a rule only SOME inputs trigger slip through as a false SAFE). So we STRIP the lines byte-identical across
    the whole bucket (the shared skeleton) and dedup on the remainder — the per-call variable.

    Dedup is WORD-SET (1-gram) Jaccard >= 0.8, deliberately NOT 3-gram: a genuinely SHORT variable (a classifier's
    one-word input under a big rubric) has no 3-grams, so a 3-gram key falls back to the whole prompt and COLLAPSES it
    to 1 — a false SAFE on the very simplest agents. A word set works from one word up. It cannot tell a genuine short
    variable from short NOISE (a session id), so it COUNTS both — the SAFE default for a prove-it tool: over-sampling a
    redundant real input is harmless; MISSING a real variation manufactures a false SAFE. This is the SAME
    shared-static / per-call split the levers optimize on: optimize the static, sample from the variable. A
    fully-identical bucket has no variable, so it correctly collapses to 1."""
    fulls = [_input_text(t) for t in traces]
    common = set.intersection(*[set(f.split("\n")) for f in fulls]) if fulls else set()  # the shared skeleton lines
    def words(s):
        return set(s.lower().split())                                   # 1-gram: robust from a single word up
    kept, kept_w = [], []
    for t, f in zip(traces, fulls):
        var = "\n".join(l for l in f.split("\n") if l not in common)     # the per-call VARIABLE (skeleton stripped)
        w = words(var) or words(f)                                      # no variable (fully static) -> full -> collapses identical
        if any((len(w & k) / max(1, len(w | k))) >= 0.8 for k in kept_w):
            continue
        kept.append(t); kept_w.append(w)
        if len(kept) >= n:
            break
    return kept


def _one_repeat(trace, produce):
    """One independent proof: the ORIGINAL's recorded behavior vs ONE fresh candidate re-run, judged once.
    `produce(trace) -> behavior string` is the TRANSFORM under test — cheaper-model replay (downgrade) OR
    reordered-prompt replay on the same model (cache). -> {preserved, reason, output}. Order-independent, so a
    node's N x K parallelize safely. (Robustness to a single bad call comes from the aggregation OVER re-runs.)"""
    b = produce(trace)
    ok, why = judge_preserved(_user_text(trace), _recorded(trace), b)
    return {"preserved": ok, "reason": why, "output": b[:AUDIT_JUDGE_MAX_CHARS]}   # full output; UI scrolls


def _verdict(cheap_kept, self_kept, k):
    """Per-INPUT verdict — strictly BINARY: SAFE or NOT-SAFE. Never 'borderline' (that's a NODE state — some inputs
    drifted but most held; see prove_transform). Judge the cheaper RELATIVE to the ORIGINAL's own self-variance
    (self_kept out of k; the recorded output is the free +1 anchor):
      - cheaper PERFECT (K/K)               -> SAFE       (can't do better)
      - cheaper NEVER matched (0/K)         -> NOT-SAFE   (no evidence it reproduces the behaviour, whatever the
                                                          original does — this is the low-is-low floor)
      - cheaper as steady as the original   -> SAFE       (cheap_kept >= self_kept: no worse than the model's own noise
                                                          — so 2/3 vs a noisy 1/3 original is SAFE, the cheaper is steadier)
      - cheaper LESS steady than original   -> NOT-SAFE   (cheap_kept < self_kept: worse than the model's own noise)
    self_kept is None when the baseline is off or k==1 -> fall back to the flat AUDIT_SAFE_RATIO rule (still binary)."""
    if cheap_kept >= k:
        return "SAFE", "held on every re-run"
    if cheap_kept == 0:                                     # never reproduced the recorded behaviour -> unsafe
        return "NOT-SAFE", "never matched the recorded output"
    if self_kept is None:                                   # no baseline (k==1 / off) -> flat ratio fallback
        return ("SAFE", "held on most re-runs") if cheap_kept >= AUDIT_SAFE_RATIO * k \
            else ("NOT-SAFE", "drifted on most re-runs")
    if cheap_kept >= self_kept:                             # as steady as (or steadier than) the original's own re-runs
        return "SAFE", "as consistent as the original's own re-runs"
    return "NOT-SAFE", "less consistent than the original's own re-runs"


def prove_transform(sample, produce, model, k=AUDIT_REPEATS, sent=None):
    """The verdict ENGINE, transform-agnostic and REUSED by every lever. `sent(trace) -> (system, user)` returns the
    TRANSFORMED input actually sent (for the report's before/after inspector); default None = input unchanged
    (downgrade). For each of the N sampled inputs, run K
    independent `produce(trace) -> behavior` candidates and judge each vs the recorded output; then, on the DOUBTFUL
    inputs only, measure the ORIGINAL model's own run-to-run noise floor (replay the ORIGINAL prompt on the ORIGINAL
    model) and judge the candidate RELATIVE to it. Returns (inputs, node_verdict, safe_count).

      downgrade: produce = replay(trace, cheaper_model)      (cheaper model, same prompt)
      cache:     produce = replay(reorder(trace), model)     (same model, reordered prompt)

    The self-variance baseline is lever-agnostic — always the original model on the original prompt."""
    tasks = [(idx, t) for idx, t in enumerate(sample) for _ in range(k)]         # N x K, all independent
    with ThreadPoolExecutor(max_workers=min(AUDIT_MAX_PARALLEL, len(tasks))) as ex:
        done = list(ex.map(lambda it: (it[0], _one_repeat(it[1], produce)), tasks))
    by_idx = {}
    for idx, res in done:
        by_idx.setdefault(idx, []).append(res)
    cand_kept = {idx: sum(1 for x in by_idx.get(idx, []) if x["preserved"]) for idx in range(len(sample))}

    # self-variance baseline — ONLY on doubtful inputs (candidate wasn't perfect). recorded output = free +1 anchor.
    self_kept, base_runs = {}, {}
    if AUDIT_SELF_BASELINE and k > 1:
        doubtful = [idx for idx in range(len(sample)) if cand_kept[idx] < k]
        btasks = [(idx, sample[idx]) for idx in doubtful for _ in range(k - 1)]
        if btasks:
            with ThreadPoolExecutor(max_workers=min(AUDIT_MAX_PARALLEL, len(btasks))) as ex:
                bdone = list(ex.map(lambda it: (it[0], _one_repeat(it[1], lambda t: replay(t, model))), btasks))
            for idx, res in bdone:
                base_runs.setdefault(idx, []).append(res)
            for idx in doubtful:
                self_kept[idx] = 1 + sum(1 for x in base_runs.get(idx, []) if x["preserved"])   # +1 = recorded anchor

    inputs = []
    for idx, t in enumerate(sample):
        s = by_idx.get(idx, [])
        ck = cand_kept[idx]
        sk = self_kept.get(idx)                       # None unless this input was doubtful and the baseline ran
        v, note = _verdict(ck, sk, k)
        drift_reason = next((x["reason"] for x in s if not x["preserved"]), "")    # surface WHY it drifted, if it did
        sysb, usrb = _system_text(t), _user_text(t)                                # BEFORE = the original input
        sysa, usra = sent(t) if sent else (sysb, usrb)                             # AFTER = the transformed input as sent
        inputs.append({"input": usrb[:AUDIT_JUDGE_MAX_CHARS], "kept": ck, "k": k, "self_kept": sk,
                       "verdict": v, "note": note, "recorded": _recorded(t)[:AUDIT_JUDGE_MAX_CHARS],
                       "samples": s, "baseline": base_runs.get(idx, []),
                       # before/after INSPECTOR fields (report tabs) — capped; full text ships in the download ZIP
                       "system_before": sysb[:_INSPECT_CHARS], "user_before": usrb[:_INSPECT_CHARS],
                       "system_after": (sysa or "")[:_INSPECT_CHARS], "user_after": (usra or "")[:_INSPECT_CHARS],
                       "reason": drift_reason or (s[0]["reason"] if s else "same decision")})
    # NODE verdict from the BINARY per-input results: SAFE if EVERY input held; NOT-SAFE if drift is the MAJORITY;
    # else BORDERLINE — some inputs drifted but MOST held ("mostly safe, your call"). $ is booked only on SAFE.
    safe = sum(1 for r in inputs if r["verdict"] == "SAFE")
    notsafe = len(inputs) - safe
    if notsafe == 0:
        verdict = "SAFE"
    elif notsafe > safe:
        verdict = "NOT-SAFE"
    else:
        verdict = "BORDERLINE"
    return inputs, verdict, safe


def audit_node(node_name, bucket, cheaper=None, n=AUDIT_SAMPLES, k=AUDIT_REPEATS, min_evidence=AUDIT_MIN_EVIDENCE):
    """Prove (or refute) a downgrade for one call-site across N inputs x K repeats. Returns the verdict dict.
    Verdicts: SAFE (recommend) · BORDERLINE (flips — don't) · NOT-SAFE (don't) · LOW-EVIDENCE (abstain) · N/A.

    `cheaper` is the target the funnel already chose (node-aware net-saving pick) — passed in so the audit re-runs
    the SAME model the report shows. When absent (CLI / direct callers) it falls back to the legacy no-mix pick."""
    model = canonical_model(bucket[0].get("model")) or bucket[0].get("model")
    if not cheaper:
        cheaper = next_cheaper(model)
    base = {"node": node_name, "model": model, "cheaper": cheaper,
            "from_tier": tier(model), "to_tier": tier(cheaper), "to_release": release(cheaper)}
    if not cheaper:
        return {**base, "verdict": "N/A", "reason": "already cheapest tier", "inputs": []}
    sample = _distinct(bucket, n)
    if len(sample) < min_evidence:
        return {**base, "verdict": "LOW-EVIDENCE", "n": len(sample), "inputs": [],
                "reason": "only %d distinct input(s), need %d" % (len(sample), min_evidence)}
    from app.services.downgrade_refset import prove_transform_refset      # lazy: avoids an import cycle
    inputs, verdict, safe = prove_transform_refset(sample, cheaper, model)
    return {**base, "verdict": verdict, "n": len(inputs), "k": AUDIT_DOWNGRADE_K, "safe_inputs": safe, "inputs": inputs}


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
