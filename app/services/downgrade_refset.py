"""Reference-set downgrade validation — the stable per-input verdict (DOWNGRADE-DESIGN.md).

No prose contract, no confidence field. The original model's 5 own outputs ARE the acceptable-behaviour range, and
"acceptable" is SELF-CALIBRATED: the cheaper may differ from the set ONLY in ways the set itself shows — so format /
structure is material iff the original holds it constant (a format-drifting original forgives the cheaper's format;
a strict-JSON original enforces it). Never a global "ignore formatting" rule.

Per input:
  original n/5  — ONE coherence call: how many of the 5 originals agree MATERIALLY (decision / facts / values, not
                  wording or format-the-original-varies).
  cheaper  n/5  — each cheaper output judged vs all 5 originals, MAJORITY VOTE (the actual decision).

Verdict (RELATIVE — a noisy-but-coherent original isn't held to perfection):
  original n/5 < COHERENCE_FLOOR       -> NOT-SAFE  (can't verify — original too inconsistent to give a stable bar)
  cheaper n/5 >= original n/5 - MARGIN  -> SAFE
  else                                  -> NOT-SAFE
Node: SAFE if >= NODE_SAFE_RATIO of inputs SAFE, else NOT-SAFE.

  coherence(request, outputs)     -> (rate, divergent_set)
  fit_vote(refset, cand, request) -> (kept, reason)      one candidate vs a reference set, majority of JUDGE_VOTES
  prove_transform_refset(sample, cheaper, original) -> (inputs, node_verdict, safe)   [report-shaped, drop-in]
"""
import math
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from app.services import llm_client
from app.services.audit import replay, _recorded, _user_text, _system_text
from app.config import (JUDGE_MODEL, AUDIT_JUDGE_MAX_CHARS, AUDIT_MAX_PARALLEL, AUDIT_PROFILE_RERUNS,
                        AUDIT_DOWNGRADE_K, AUDIT_JUDGE_VOTES, AUDIT_COHERENCE_FLOOR, AUDIT_DOWNGRADE_MARGIN,
                        AUDIT_NODE_SAFE_RATIO, AUDIT_JUDGE_MAX_TOKENS, AUDIT_JUDGE_REF_CHARS)

CAP = AUDIT_JUDGE_MAX_CHARS
OUT = AUDIT_JUDGE_REF_CHARS             # per-output cap when packing reference outputs into a judge prompt


def _cap(s, n):
    """Slice for a judge prompt, but NEVER silently: when we cut, SAY so in-prompt, so the judge knows its view is
    partial (and _oversize below flags the input unverified rather than letting a truncated compare read as SAFE)."""
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + "\n…[TRUNCATED %d chars — output exceeds the judge's %dk-char budget]" % (len(s) - n, n // 1000)


def _oversize(originals, cheaper):
    """True if any output is too long to show the judge WHOLE — references are packed at OUT, candidates at CAP.
    When true we can't fully verify the input, so we refuse to certify (never a false SAFE from a clipped view)."""
    return any(len(o or "") > OUT for o in originals) or any(len(c or "") > CAP for c in cheaper)


def _pmap(fn, items):
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(AUDIT_MAX_PARALLEL, len(items))) as ex:
        return list(ex.map(fn, items))


def _req(t):
    """The judge's view of the request: SYSTEM prompt + USER prompt (capped). The system prompt gives the judge the
    rules, so it knows WHY a difference is material; identical across a node's judge calls -> prompt-cacheable."""
    return ("SYSTEM:\n%s\n\nUSER:\n%s" % (_system_text(t), _user_text(t)))[:CAP]


# ── The fit judge: does a candidate belong to the reference set? Materiality SELF-CALIBRATED by the set. ────────────
_FIT_SYS = ("You decide whether a CANDIDATE output belongs to the same behaviour as a REFERENCE SET of outputs the "
            "model already considers acceptable for the same request.")
_FIT_TMPL = (
    "REQUEST:\n%s\n\n"
    "REFERENCE SET — %d outputs the model considers ACCEPTABLE for this request:\n%s\n\n"
    "CANDIDATE:\n%s\n\n"
    "Does the CANDIDATE carry the SAME decision / classification / required facts / values / tool call as the set, "
    "differing ONLY in ways the set ITSELF already shows? Judge material CONTENT, not surface form:\n"
    "- Wording and ordering differences are always fine.\n"
    "- A FORMAT or STRUCTURE difference (e.g. JSON vs plain text) is fine ONLY IF the reference set itself varies "
    "that way. If every output in the set holds one form, a candidate that breaks it does NOT belong.\n"
    "- A changed decision, a dropped / added material fact or value, or an invented / contradicted claim NEVER "
    "belongs.\n"
    "- If unsure -> BROKE.\n"
    "Think in ONE short line (<=15 words), THEN on the FINAL line output exactly KEPT or BROKE."
)


def _fit_once(refset, cand, request):
    ref = "\n\n".join("--- acceptable %d ---\n%s" % (i + 1, _cap(o, OUT)) for i, o in enumerate(refset))
    p = _FIT_TMPL % (request[:CAP], len(refset), ref, _cap(cand, CAP))
    r = llm_client.complete(model=JUDGE_MODEL, max_tokens=AUDIT_JUDGE_MAX_TOKENS, system=_FIT_SYS,
                            messages=[{"role": "user", "content": p}])
    raw = "".join(x.text for x in r.content if x.type == "text")
    hits = list(re.finditer(r"\b(KEPT|BROKE)\b", raw, flags=re.I))
    kept = bool(hits) and hits[-1].group(1).upper() == "KEPT"
    reason = next((l.strip() for l in raw.splitlines()
                   if l.strip() and not re.fullmatch(r"(KEPT|BROKE)[\s:.\-]*", l.strip(), flags=re.I)),
                  "kept" if kept else "broke")
    return kept, reason[:240]


def fit_vote(refset, cand, request, votes=None):
    """Judge `cand` against `refset` `votes` times; MAJORITY KEPT wins. Returns (kept, reason)."""
    votes = votes or AUDIT_JUDGE_VOTES
    res = _pmap(lambda _: _fit_once(refset, cand, request), range(votes))
    kept = sum(1 for k, _ in res if k) * 2 > len(res)
    reason = next((why for k, why in res if k == kept), res[0][1] if res else "")
    return kept, reason


# ── Original self-consistency: ONE holistic call. Same material-content, self-calibrated standard. ────────────────
_COH_SYS = "You assess how consistent several outputs from the SAME model are for the SAME request."
_COH_TMPL = (
    "REQUEST:\n%s\n\n"
    "Here are %d outputs the SAME model produced for this request:\n%s\n\n"
    "How many carry the SAME decision / classification / required facts / values / tool call as one another? Judge "
    "material CONTENT, not surface form — wording, ordering, and format differences do NOT count as disagreement "
    "UNLESS they change the meaning; a changed decision or a different material value DOES.\n"
    "Line 1 — output exactly: CONSISTENT n/%d\n"
    "Line 2 — output: DIVERGENT followed by the numbers of the outputs that break from the majority "
    "(e.g. 'DIVERGENT 2,4'), or 'DIVERGENT none'."
)


def coherence(request, outputs):
    """One call -> (rate, divergent_set): how many of `outputs` agree materially, and which NUMBERS (1-based) diverge."""
    ref = "\n\n".join("--- output %d ---\n%s" % (i + 1, _cap(o, OUT)) for i, o in enumerate(outputs))
    total = len(outputs)
    r = llm_client.complete(model=JUDGE_MODEL, max_tokens=AUDIT_JUDGE_MAX_TOKENS, system=_COH_SYS,
                            messages=[{"role": "user", "content": _COH_TMPL % (request[:CAP], total, ref, total)}])
    raw = "".join(x.text for x in r.content if x.type == "text")
    dm = re.search(r"DIVERGENT[:\s]+([0-9,\s]+)", raw, flags=re.I)
    if dm:                                                    # divergent list is authoritative -> rate = total - |div|
        div = {d for d in (int(x) for x in re.findall(r"\d+", dm.group(1))) if 1 <= d <= total}
        return total - len(div), div
    m = re.search(r"(\d+)\s*/\s*(\d+)", raw)                  # fallback: the n/N count, can't say which diverged
    return (max(0, min(int(m.group(1)), total)) if m else total), set()


def prove_transform_refset(sample, cheaper, original, k=None):
    """The engine. Per input: 5 original outputs (recorded + reruns) = the set; ONE coherence call for the original's
    self-consistency; run cheaper K times and majority-vote each against the set; verdict = relative comparison.
    Returns (inputs, node_verdict, safe) in the shape the report renders."""
    k = k or AUDIT_DOWNGRADE_K
    n = len(sample)
    votes = AUDIT_JUDGE_VOTES
    req = {idx: _req(sample[idx]) for idx in range(n)}

    # Phase A — original re-runs -> the 5-output reference set.
    a_tasks = [(idx, t) for idx, t in enumerate(sample) for _ in range(AUDIT_PROFILE_RERUNS)]
    reruns = defaultdict(list)
    for idx, out in _pmap(lambda it: (it[0], replay(it[1], original)), a_tasks):
        reruns[idx].append(out)
    originals = {idx: [_recorded(sample[idx])] + reruns[idx] for idx in range(n)}

    # Phase B — cheaper re-runs.
    b_tasks = [(idx, t) for idx, t in enumerate(sample) for _ in range(k)]
    cheap = defaultdict(list)
    for idx, out in _pmap(lambda it: (it[0], replay(it[1], cheaper)), b_tasks):
        cheap[idx].append(out)

    # Phase C1 — ORIGINAL self-consistency: ONE coherence call per input (the bar). No voting.
    coh = dict(_pmap(lambda idx: (idx, coherence(req[idx], originals[idx])), list(range(n))))

    # Phase C2 — CHEAPER fit: each cheaper output vs all 5 originals, MAJORITY VOTE (the decision).
    jobs = [("cheap", idx, j) for idx in range(n) for j in range(len(cheap[idx])) for _ in range(votes)]
    done = _pmap(lambda job: (job[1], job[2], _fit_once(originals[job[1]], cheap[job[1]][job[2]], req[job[1]])), jobs)
    tally, reasons = defaultdict(list), defaultdict(list)
    for idx, j, (kept, why) in done:
        tally[(idx, j)].append(kept)
        reasons[(idx, j)].append(why)

    def majority(idx, j):
        vs, rs = tally[(idx, j)], reasons[(idx, j)]
        yes = sum(1 for v in vs if v)
        kept = yes * 2 > len(vs)
        why = next((w for v, w in zip(vs, rs) if v == kept), rs[0])
        judges = [{"kept": v, "reason": w} for v, w in zip(vs, rs)]
        return kept, why, "%d/%d" % (yes if kept else len(vs) - yes, len(vs)), judges

    inputs = []
    for idx, t in enumerate(sample):
        O, C = originals[idx], cheap[idx]
        orig_rate, divergent = coh[idx]                          # from the one coherence call
        no = len(O)
        cheap_res = [majority(idx, j) for j in range(len(C))]
        cheap_rate, nc = sum(1 for kp, _, _, _ in cheap_res if kp), len(C)

        unverified = _oversize(O, C)                             # any output too long for the judge to read whole
        if unverified:                                           # honest refusal — never certify on a truncated view
            verdict = "NOT-SAFE"
            note = ("couldn't fully verify — an output here exceeds the judge's %dk-char budget, so the comparison "
                    "would run on a truncated view; not certifying a downgrade" % (OUT // 1000))
        elif orig_rate < AUDIT_COHERENCE_FLOOR:                  # original too inconsistent to verify against -> abstain
            verdict = "NOT-SAFE"
            note = ("couldn't verify — your current model gave different answers here (%d/%d consistent), so there is "
                    "no stable behaviour to certify a downgrade against" % (orig_rate, no))
        elif cheap_rate >= orig_rate - AUDIT_DOWNGRADE_MARGIN:   # cheaper stays within the original's own range
            verdict = "SAFE"
            note = "cheaper stays within the original's own range (%d/%d vs original %d/%d)" % (cheap_rate, nc, orig_rate, no)
        else:
            verdict = "NOT-SAFE"
            note = "cheaper less consistent than the original (%d/%d vs original %d/%d)" % (cheap_rate, nc, orig_rate, no)

        inputs.append({
            "input": _user_text(t)[:CAP], "system": _system_text(t)[:CAP], "recorded": _recorded(t)[:CAP],
            "kept": cheap_rate, "k": nc, "verdict": verdict, "note": note, "unverified": unverified,
            "self_rate": "%d/%d" % (orig_rate, no),
            "orig_runs": [{"preserved": (i + 1) not in divergent, "reason": "", "votes": "",
                           "output": (O[i] or "")[:CAP]} for i in range(no)],   # fits/differs from the coherence call
            "samples": [{"preserved": kp, "reason": why, "votes": vt, "judges": jd, "output": (C[j] or "")[:CAP]}
                        for j, (kp, why, vt, jd) in enumerate(cheap_res)],
        })

    # NODE roll-up: SAFE if enough inputs SAFE, else NOT-SAFE.
    safe = sum(1 for r in inputs if r["verdict"] == "SAFE")
    need = math.ceil(AUDIT_NODE_SAFE_RATIO * n) if n else 1
    node = "SAFE" if (inputs and safe >= need) else "NOT-SAFE"
    return inputs, node, safe
