"""The validation engine: does a candidate model behave like the original on a request?

Per item, the original's recorded answer plus `reference_runs` fresh re-runs are the REFERENCE SET: the range of
behaviour the original itself shows. One coherence judgement says how consistent that set is. Each candidate is
re-run `candidate_runs` times and every output is judged, by majority of `votes`, on whether it belongs to the set.
Verdict per candidate, relative to the original's own consistency:

    unverified  a call failed, an output is too long to judge whole, the coherence reply was unreadable, or the
                original agrees with itself less than `coherence_floor` of the time (nothing stable to certify against)
    within      fit rate >= original's consistency - margin
    outside     otherwise

`judge_reason` is the judge's most common reason behind the verdict (e.g. "urgency changed from medium to high").
The engine never picks a winner. It reports every run with its latency, tokens, served model and cost, so the
caller can rank candidates by whatever it values.
"""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from validation import judges
from validation.pricing import cost_of


def _pmap(fn, items, width):
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(width, len(items))) as ex:
        return list(ex.map(fn, items))


def _text(trace, role, sep="\n"):
    return sep.join((m.get("content") or "") for m in trace.get("input_messages", []) if m.get("role") == role)


def request_text(trace, spec):
    """The judge's view of the request (system + user text). Identical across an item's judge calls: the cached prefix."""
    return ("SYSTEM:\n%s\n\nUSER:\n%s" % (_text(trace, "system"), _text(trace, "user", " ")))[:spec.cap_chars]


def _default_run(trace, model):
    from app.services.audit import replay
    return replay(trace, model)


def _call(run, trace, model):
    """One model call -> run record. A failure is a record too (the item becomes unverified), never an exception —
    except an unreachable endpoint or a refused account (out of credit, over budget), which say nothing about the
    model: raised for the caller to wait or stop instead of recording unverified results."""
    try:
        r = dict(run(trace, model))
    except Exception as e:
        from app.services.llm_client import Refused, Unreachable
        if isinstance(e, (Unreachable, Refused)):
            raise
        return {"output": "", "error": str(e)[:200], "model": model, "usage": None, "latency_ms": None, "cost_usd": None}
    r.setdefault("model", model)
    r["cost_usd"] = cost_of(r.get("model"), r.get("usage"))
    return r


ANSWERS = ("match", "acceptable", "drift", "unclear")       # what each candidate answer was judged


def _recorded_run(item):
    u = item.request.get("usage") or {}
    usage = {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
             "cache_read": u.get("cached_tokens", 0), "cache_write": 0} if u else None
    return {"output": item.recorded, "recorded": True, "model": item.model, "usage": usage,
            "latency_ms": item.request.get("runtime_ms") or None, "cost_usd": cost_of(item.model, usage)}


def _verdict(kept, nruns, failed, oversize, coh, spec):
    if failed:
        return "unverified", "%d of %d candidate calls failed" % (failed, nruns)
    if oversize:
        return "unverified", ("an output exceeds the judge's %dk-char budget, so it can't be compared whole"
                              % (spec.ref_chars // 1000))
    if coh["agree"] is None:
        return "unverified", "the original's consistency could not be read from the judge's reply"
    orig_rate, fit_rate = coh["agree"] / coh["of"], kept / nruns
    if orig_rate < spec.coherence_floor - 1e-9:
        return "unverified", ("the original gave different answers here (%d/%d consistent); no stable behaviour to "
                              "certify against" % (coh["agree"], coh["of"]))
    rates = "(%d/%d vs original %d/%d)" % (kept, nruns, coh["agree"], coh["of"])
    if fit_rate + 1e-9 >= orig_rate - spec.margin:
        return "within", "stays within the original's own range " + rates
    return "outside", "less consistent than the original " + rates


def validate_many(items, spec, run=None):
    """[Item] -> [result]. Two pools across ALL items: every model call, then every judge call (so N items with M
    candidates keep the provider busy `max_parallel` wide instead of running item by item)."""
    run = run or _default_run
    calls = []                                                  # (item index, "ref" | candidate index, model)
    for i, it in enumerate(items):
        calls += [(i, "ref", it.model)] * (spec.reference_runs + (0 if it.recorded else 1))
        for c, cand in enumerate(it.candidates):
            calls += [(i, c, cand.model)] * spec.candidate_runs
    runs = defaultdict(list)
    for (i, who, _), r in zip(calls, _pmap(lambda x: _call(run, items[x[0]].request, x[2]), calls, spec.max_parallel)):
        runs[(i, who)].append(r)
    reference = {i: ([_recorded_run(it)] if it.recorded else []) + runs[(i, "ref")] for i, it in enumerate(items)}
    reqs = {i: request_text(it.request, spec) for i, it in enumerate(items)}
    ref_ok = {i: not any(r.get("error") for r in reference[i]) for i in range(len(items))}

    jobs = [("coh", i, None, None) for i in range(len(items)) if ref_ok[i]]
    jobs += [("fit", i, c, j) for i, it in enumerate(items) if ref_ok[i] for c in range(len(it.candidates))
             for j in range(spec.candidate_runs) if not runs[(i, c)][j].get("error") for _ in range(spec.votes)]

    def judge(job):
        from app.services.llm_client import Refused, Unreachable
        kind, i, c, j = job
        ref_out = [r["output"] for r in reference[i]]
        try:
            if kind == "coh":
                return judges.coherence(reqs[i], ref_out, spec)
            return judges.fit(ref_out, runs[(i, c)][j]["output"], reqs[i], spec)
        except (Unreachable, Refused):
            raise
        except Exception as e:                                  # e.g. a content filter on this one request
            why = "a judge call failed: %s" % str(e)[:200]
            if kind == "coh":
                return {"agree": None, "of": len(ref_out), "divergent": set(), "reasons": {}, "cost_usd": 0.0,
                        "error": why}
            return None, why, 0.0                               # no vote: the run counts as not judged
    verdicts = defaultdict(list)
    for job, v in zip(jobs, _pmap(judge, jobs, spec.max_parallel)):
        verdicts[job[:3]].append(v)

    out = []
    for i, it in enumerate(items):
        ref = reference[i]
        coh = (verdicts[("coh", i, None)] or [{"agree": None, "of": len(ref), "divergent": set(), "reasons": {},
                                                "cost_usd": 0.0}])[0]
        spend = sum((r["cost_usd"] or 0) for r in ref if not r.get("recorded")) + coh["cost_usd"]
        ref_over = any(len(r["output"] or "") > spec.ref_chars for r in ref)
        cands = []
        for c, cand in enumerate(it.candidates):
            cruns, rows, kept = runs[(i, c)], [], 0
            unjudged = None
            for j, r in enumerate(cruns):
                votes = verdicts[("fit", i, c)][j * spec.votes:(j + 1) * spec.votes] if not r.get("error") else []
                decided = [k for k, _, _ in votes if k is not None]      # unsure or failed votes abstain
                if votes and len(decided) * 2 <= len(votes):            # most abstained: this run isn't judged
                    unjudged = unjudged or next(w for k, w, _ in votes if k is None)
                yes = sum(1 for k in decided if k)
                ok = len(decided) * 2 > len(votes) and yes * 2 > len(decided)
                loose = sum(1 for k, w, _ in votes if k and w.startswith("acceptable:"))
                category = ("unclear" if r.get("error") or not votes or len(decided) * 2 <= len(votes) else
                            ("acceptable" if loose * 2 > yes else "match") if ok else "drift")
                kept += ok
                spend += (r["cost_usd"] or 0) + sum(cost for _, _, cost in votes)
                rows.append({**{k: r.get(k) for k in ("output", "model", "usage", "latency_ms", "cost_usd", "error",
                                                       "degraded", "stop_reason")},
                             "kept": ok, "category": category,
                             "votes": "%d/%d" % (yes if ok else len(decided) - yes, len(decided)) if votes else "",
                             "reason": next((w for k, w, _ in votes if k == ok), votes[0][1] if votes else
                                            (r.get("error") or "")),
                             "judges": [{"kept": k, "reason": w} for k, w, _ in votes]})
            failed = sum(1 for r in cruns if r.get("error")) + (0 if ref_ok[i] else len(cruns))
            over = ref_over or any(len(r["output"] or "") > spec.cap_chars for r in cruns)
            verdict, reason = _verdict(kept, len(cruns), failed, over, coh, spec)
            if ref_ok[i] and (coh.get("error") or unjudged):           # a judge call failed: nothing to certify
                verdict, reason = "unverified", coh.get("error") or unjudged
            if not ref_ok[i]:
                reason = "a reference re-run of the original failed: " + next(r["error"] for r in ref if r.get("error"))
            decisive = [x["reason"] for x in rows if x["reason"] and x["kept"] == (verdict == "within")]
            timed = [r["latency_ms"] for r in cruns if r.get("latency_ms")]
            priced = [r["cost_usd"] for r in cruns if r.get("cost_usd") is not None]
            answers = Counter(x["category"] for x in rows)
            cands.append({"label": cand.label, "model": cand.model, "kept": kept, "of": len(cruns),
                          "answers": {k: answers.get(k, 0) for k in ANSWERS},
                          "fit_rate": round(kept / len(cruns), 3) if cruns else None,
                          "verdict": verdict, "reason": reason, "runs": rows,
                          "judge_reason": Counter(decisive).most_common(1)[0][0] if decisive else "",
                          "latency_ms": round(sum(timed) / len(timed)) if timed else None,
                          "cost_usd": round(sum(priced) / len(priced), 6) if priced else None})
        out.append({"id": it.id, "model": it.model,
                    "reference": {"agree": coh["agree"], "of": len(ref),
                                  "consistency": round(coh["agree"] / len(ref), 3) if coh["agree"] is not None and ref
                                  else None,
                                  "runs": [{**{k: r.get(k) for k in ("output", "model", "usage", "latency_ms",
                                                                      "cost_usd", "error", "recorded", "stop_reason")},
                                            "agrees": (n + 1) not in coh["divergent"],
                                            "reason": coh["reasons"].get(n + 1, "")} for n, r in enumerate(ref)]},
                    "candidates": cands, "spend_usd": round(spend, 6)})
    return out


def validate(item, spec, run=None):
    return validate_many([item], spec, run)[0]
