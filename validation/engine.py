"""The validation engine: does a candidate model behave like the original on a request?

The original's recorded answer plus `reference_runs` fresh re-runs form the reference set, the range of behaviour the
original itself shows; one coherence judgement says how consistent it is. Each of `candidate_runs` candidate outputs
is judged on whether it belongs to the set: an exact copy of a reference matches with no judge call; otherwise one
judge vote, and when it isn't a pass `votes` - 1 more, the majority deciding. The judge reads the whole request and
every output, never a cut. Verdict, relative to the original's consistency:

    unverified  a call failed (one too large for the judge included), the coherence reply was unreadable, or the
                original agrees with itself less than `coherence_floor` of the time (nothing stable to certify against)
    within      fit rate >= original's consistency - margin
    outside     otherwise

No winner is picked: every run is reported with latency, tokens, served model and cost for the caller to rank.
"""
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from validation import judges
from validation.pricing import cost_of


def _pmap(fn, items, width):
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(width, len(items))) as ex:
        return list(ex.map(fn, items))


def _pmap_warm(fn, items, key, width):
    """_pmap with the first item of each `key` run before the rest: calls that share a prompt prefix then read the
    provider's prompt cache instead of all writing it at once."""
    seen, first, rest = set(), [], []
    for n, x in enumerate(items):
        (rest if key(x) in seen else first).append(n)
        seen.add(key(x))
    out = [None] * len(items)
    for batch in (first, rest):
        for n, r in zip(batch, _pmap(lambda n: fn(items[n]), batch, width)):
            out[n] = r
    return out


def _said(content):
    """A message's content as text: parts joined, a part with no text named by its type."""
    if isinstance(content, list):
        return "\n".join(p.get("text") or "[%s]" % p.get("type", "part") if isinstance(p, dict) else str(p)
                         for p in content)
    return content if isinstance(content, str) else "" if content is None else json.dumps(content)


_ROLE = {"system": "SYSTEM", "developer": "SYSTEM", "user": "USER", "assistant": "ASSISTANT", "tool": "TOOL RESULT",
         "function": "TOOL RESULT"}


def request_text(trace):
    """The judge's view of the request, whole: every turn in order with its tool calls and results, the tool
    definitions after the instructions. The same in all of an item's judge calls, so cached."""
    sent = trace.get("logged_request") or {}
    msgs = sent.get("messages") or trace.get("input_messages") or []
    tools = ([json.dumps(t.get("function", t), ensure_ascii=False) for t in sent.get("tools") or []] if sent else
             [schema for _, schema in trace.get("tools_defined") or []])
    parts = []
    for m in msgs:
        calls = "".join("\n[calls %s(%s)]" % (c.get("function", {}).get("name"), c.get("function", {}).get("arguments"))
                        for c in m.get("tool_calls") or [])
        parts.append("%s:\n%s%s" % (_ROLE.get(m.get("role"), str(m.get("role")).upper()), _said(m.get("content")),
                                     calls))
    if tools:
        lead = next((n for n, m in enumerate(msgs) if m.get("role") not in ("system", "developer")), len(msgs))
        parts.insert(lead, "TOOLS the model could call:\n" + "\n".join(tools))
    return "\n\n".join(parts)


def _default_run(trace, model):
    from app.services.audit import replay
    return replay(trace, model)


def _call(run, trace, model):
    """One model call -> run record; a failure is a record too (the item becomes unverified). An unreachable endpoint
    or a refused account says nothing about the model, so it raises for the caller to wait or stop."""
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
_TOO_LARGE = ("context length", "context_length", "context window", "maximum context", "prompt is too long",
              "input is too long", "too many tokens")               # a judge call's error when the prompt doesn't fit


def _recorded_run(item):
    u = item.request.get("usage") or {}
    cached = u.get("cached_tokens", 0) or 0                    # the trace's input_tokens include the cached ones
    usage = {"input": max(0, (u.get("input_tokens", 0) or 0) - cached), "output": u.get("output_tokens", 0),
             "cache_read": cached, "cache_write": 0} if u else None
    return {"output": item.recorded, "recorded": True, "model": item.model, "usage": usage,
            "latency_ms": item.request.get("runtime_ms") or None, "cost_usd": cost_of(item.model, usage)}


def _verdict(kept, nruns, failed, coh, spec):
    if failed:
        return "unverified", "%d of %d candidate calls failed" % (failed, nruns)
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
    """[Item] -> [result]. Two pools across all items, every model call then every judge call, `max_parallel` wide."""
    run = run or _default_run
    calls = []                                                  # (item index, "ref" | candidate index, model)
    for i, it in enumerate(items):
        calls += [(i, "ref", it.model)] * (spec.reference_runs + (0 if it.recorded else 1))
        for c, cand in enumerate(it.candidates):
            calls += [(i, c, cand.model)] * spec.candidate_runs
    runs = defaultdict(list)
    # all at once, as production would send them: no order of ours gives one model's re-runs a cache discount
    for (i, who, _), r in zip(calls, _pmap(lambda x: _call(run, items[x[0]].request, x[2]), calls, spec.max_parallel)):
        runs[(i, who)].append(r)
    reference = {i: ([_recorded_run(it)] if it.recorded else []) + runs[(i, "ref")] for i, it in enumerate(items)}
    reqs = {i: request_text(it.request) for i, it in enumerate(items)}
    ref_ok = {i: not any(r.get("error") for r in reference[i]) for i in range(len(items))}
    copies = {(i, c, j) for i, it in enumerate(items) for c in range(len(it.candidates))
              for j, r in enumerate(runs[(i, c)]) if not r.get("error")
              and (r["output"] or "").strip() in {(x["output"] or "").strip() for x in reference[i]}}

    jobs = [("coh", i, None, None) for i in range(len(items)) if ref_ok[i]]
    jobs += [("fit", i, c, j) for i, it in enumerate(items) if ref_ok[i] for c in range(len(it.candidates))
             for j in range(spec.candidate_runs) if not runs[(i, c)][j].get("error") and (i, c, j) not in copies]

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
            big = any(w in str(e).lower() for w in _TOO_LARGE)
            why = ("the request and its answers are too large for the judge (%s)" % spec.judge_model if big else
                   "a judge call failed: %s" % str(e)[:200])
            if kind == "coh":
                return {"agree": None, "of": len(ref_out), "divergent": set(), "reasons": {}, "cost_usd": 0.0,
                        "error": why}
            return None, why, 0.0                               # no vote: the run counts as not judged
    verdicts = defaultdict(list)                                # job -> its votes (coherence: its one reply)
    for job, v in zip(jobs, _pmap_warm(judge, jobs, lambda j: j[:2], spec.max_parallel)):     # one prefix per item
        verdicts[job].append(v)
    more = [job for job in jobs if job[0] == "fit" and verdicts[job][0][0] is not True for _ in range(spec.votes - 1)]
    for job, v in zip(more, _pmap(judge, more, spec.max_parallel)):        # not a pass: more votes, majority decides
        verdicts[job].append(v)

    out = []
    for i, it in enumerate(items):
        ref = reference[i]
        coh = (verdicts[("coh", i, None, None)] or [{"agree": None, "of": len(ref), "divergent": set(), "reasons": {},
                                                "cost_usd": 0.0}])[0]
        spend = sum((r["cost_usd"] or 0) for r in ref if not r.get("recorded")) + coh["cost_usd"]
        cands = []
        for c, cand in enumerate(it.candidates):
            cruns, rows, kept = runs[(i, c)], [], 0
            unjudged = None
            for j, r in enumerate(cruns):
                copy = (i, c, j) in copies
                votes = verdicts[("fit", i, c, j)]
                decided = [k for k, _, _ in votes if k is not None]      # unsure or failed votes abstain
                if votes and len(decided) * 2 <= len(votes):            # most abstained: this run isn't judged
                    unjudged = unjudged or next(w for k, w, _ in votes if k is None)
                yes = sum(1 for k in decided if k)
                ok = copy or (len(decided) * 2 > len(votes) and yes * 2 > len(decided))
                loose = sum(1 for k, w, _ in votes if k and w.startswith("acceptable:"))
                category = ("match" if copy else
                            "unclear" if r.get("error") or not votes or len(decided) * 2 <= len(votes) else
                            ("acceptable" if loose * 2 > yes else "match") if ok else "drift")
                kept += ok
                spend += (r["cost_usd"] or 0) + sum(cost for _, _, cost in votes)
                rows.append({**{k: r.get(k) for k in ("output", "model", "usage", "latency_ms", "cost_usd", "error",
                                                       "degraded", "stop_reason")},
                             "kept": ok, "category": category,
                             "votes": "%d/%d" % (yes if ok else len(decided) - yes, len(decided)) if votes else "",
                             "reason": "identical to one of the original's answers" if copy else
                                       next((w for k, w, _ in votes if k == ok), votes[0][1] if votes else
                                            (r.get("error") or "")),
                             "judges": [{"kept": k, "reason": w} for k, w, _ in votes]})
            failed = sum(1 for r in cruns if r.get("error")) + (0 if ref_ok[i] else len(cruns))
            verdict, reason = _verdict(kept, len(cruns), failed, coh, spec)
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
