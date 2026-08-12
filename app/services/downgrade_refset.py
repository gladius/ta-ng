"""Reference-set downgrade validation — the stable per-input verdict (DOWNGRADE-STABILITY-LOGIC.md).

No prose contract, no confidence field. The original model's 5 own outputs ARE the acceptable-behaviour range; we
hand the judge those real outputs and ask "does this candidate belong to the set?". Every judgement is a MAJORITY
VOTE (a single judge coin-flips on borderline outputs — measured). Per input we produce two rates by the IDENTICAL
mechanism, so comparing them is apples-to-apples:

  original n/5  — each original output judged against the OTHER 4 (leave-one-out): the original's self-consistency.
  cheaper  n/5  — each cheaper output judged against ALL 5 originals: the cheaper's fit.

Verdict (arithmetic, deterministic given the two numbers — so it can't flip):
  original n/5 < COHERENCE_FLOOR          -> NOT-SAFE (can't certify — the original is inconsistent with itself)
  cheaper n/5 >= original n/5 - MARGIN     -> SAFE     (cheaper no worse than the original's own rate)
  else                                     -> NOT-SAFE
Node: SAFE if at least NODE_SAFE_RATIO of inputs are SAFE (softer than all-must-pass, which amplifies noise).

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
                        AUDIT_NODE_SAFE_RATIO, AUDIT_JUDGE_MAX_TOKENS, AUDIT_PROFILER_SAMPLE_CHARS)

CAP = AUDIT_JUDGE_MAX_CHARS
OUT = AUDIT_PROFILER_SAMPLE_CHARS       # per-output cap when packing the reference set into the prompt


def _pmap(fn, items):
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(AUDIT_MAX_PARALLEL, len(items))) as ex:
        return list(ex.map(fn, items))


# ── The ONE judge: does a candidate belong to the reference set? (used for BOTH original leave-one-out and cheaper) ──
_FIT_SYS = ("You decide whether a CANDIDATE output belongs to the same behaviour as a REFERENCE SET of outputs the "
            "model already considers acceptable for the same request.")
_FIT_TMPL = (
    "REQUEST:\n%s\n\n"
    "REFERENCE SET — %d outputs the model considers ACCEPTABLE for this request:\n%s\n\n"
    "CANDIDATE:\n%s\n\n"
    "Is the CANDIDATE the SAME KIND of answer as the set — same decision / classification / required facts / tool "
    "call / structure — staying within the variation the set already shows? IGNORE wording, order, and cosmetic "
    "formatting. A changed decision, a dropped or added material fact, or an invented / contradicted claim is NOT "
    "within range. If unsure -> BROKE.\n"
    "Think in ONE short line (<=15 words), THEN on the FINAL line output exactly KEPT or BROKE."
)


def _fit_once(refset, cand, request):
    ref = "\n\n".join("--- acceptable %d ---\n%s" % (i + 1, (o or "")[:OUT]) for i, o in enumerate(refset))
    p = _FIT_TMPL % (request[:CAP], len(refset), ref, (cand or "")[:CAP])
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


def prove_transform_refset(sample, cheaper, original, k=None):
    """Reference-set engine. Per input: 5 original outputs (recorded + reruns) = the set; run cheaper K times; judge
    (majority-vote) each original vs the other 4 AND each cheaper vs all 5; verdict = arithmetic on the two rates.
    All individual judge calls are flattened into ONE bounded pool. Returns (inputs, node_verdict, safe) in the shape
    the report already renders."""
    k = k or AUDIT_DOWNGRADE_K
    n = len(sample)
    votes = AUDIT_JUDGE_VOTES

    # Phase A — original re-runs -> the 5-output reference set per input.
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

    # Phase C — ONE flat pool of every individual judge call (votes included). kind: "orig" leave-one-out / "cheap".
    req = {idx: _user_text(sample[idx]) for idx in range(n)}
    jobs = []
    for idx in range(n):
        O = originals[idx]
        for i in range(len(O)):                                  # each original vs the OTHER originals
            refset = O[:i] + O[i + 1:]
            jobs += [("orig", idx, i, refset, O[i])] * votes
        for j, c in enumerate(cheap[idx]):                       # each cheaper vs ALL originals
            jobs += [("cheap", idx, j, O, c)] * votes
    done = _pmap(lambda job: (job[0], job[1], job[2], _fit_once(job[3], job[4], req[job[1]])), jobs)

    tally, reasons = defaultdict(list), defaultdict(list)
    for kind, idx, i, (kept, why) in done:
        tally[(kind, idx, i)].append(kept)
        reasons[(kind, idx, i)].append(why)

    def majority(kind, idx, i):
        vs, rs = tally[(kind, idx, i)], reasons[(kind, idx, i)]
        yes = sum(1 for v in vs if v)
        kept = yes * 2 > len(vs)
        why = next((w for v, w in zip(vs, rs) if v == kept), rs[0])
        judges = [{"kept": v, "reason": w} for v, w in zip(vs, rs)]              # every individual vote (UI)
        return kept, why, "%d/%d" % (yes if kept else len(vs) - yes, len(vs)), judges

    inputs = []
    for idx, t in enumerate(sample):
        O, C = originals[idx], cheap[idx]
        orig_res = [majority("orig", idx, i) for i in range(len(O))]
        cheap_res = [majority("cheap", idx, j) for j in range(len(C))]
        orig_rate, no = sum(1 for kp, _, _, _ in orig_res if kp), len(O)
        cheap_rate, nc = sum(1 for kp, _, _, _ in cheap_res if kp), len(C)

        if orig_rate < AUDIT_COHERENCE_FLOOR:
            verdict = "NOT-SAFE"
            note = "original inconsistent with itself (%d/%d) — can't certify a downgrade" % (orig_rate, no)
        elif cheap_rate >= orig_rate - AUDIT_DOWNGRADE_MARGIN:
            verdict = "SAFE"
            note = "cheaper as consistent as the original (%d/%d vs original %d/%d)" % (cheap_rate, nc, orig_rate, no)
        else:
            verdict = "NOT-SAFE"
            note = "cheaper less consistent than the original (%d/%d vs original %d/%d)" % (cheap_rate, nc, orig_rate, no)

        inputs.append({
            "input": _user_text(t)[:CAP], "system": _system_text(t)[:CAP], "recorded": _recorded(t)[:CAP],
            "kept": cheap_rate, "k": nc, "verdict": verdict, "note": note,
            "self_rate": "%d/%d" % (orig_rate, no),
            "orig_runs": [{"preserved": kp, "reason": why, "votes": vt, "judges": jd, "output": (O[i] or "")[:CAP]}
                          for i, (kp, why, vt, jd) in enumerate(orig_res)],    # each original's leave-one-out
            "samples": [{"preserved": kp, "reason": why, "votes": vt, "judges": jd, "output": (C[j] or "")[:CAP]}
                        for j, (kp, why, vt, jd) in enumerate(cheap_res)],
        })

    # NODE — softer than all-must-pass: SAFE if at least NODE_SAFE_RATIO of inputs are SAFE.
    safe = sum(1 for r in inputs if r["verdict"] == "SAFE")
    need = math.ceil(AUDIT_NODE_SAFE_RATIO * n) if n else 1
    node = "SAFE" if (inputs and safe >= need) else "NOT-SAFE"
    return inputs, node, safe
