"""Honesty guards over the analyst's read — the Phase-2 trust layer. Deterministic reconciliation against the
fact-pack: a citation that doesn't exist is dropped; the varying-frame fact is surfaced whatever the read implies;
a possible-mixed flag is kept visible; a one-sample read is marked thin. NO LLM here — this only checks the read
against provable facts. Understanding-first: guards make the read TRUE, they don't score it.
"""


def _quote_exists(quote, bucket, trace_id=None):
    """Is the analyst's cited quote actually present in the traces? (anti-hallucination). Prefer the cited trace,
    fall back to any trace in the bucket."""
    q = (quote or "").strip().lower()
    if len(q) < 4:
        return False
    def _blob(t):
        s = " ".join((m.get("content") or "") for m in t.get("input_messages", []))
        return (s + " " + str(t.get("output") or "")).lower()
    if trace_id is not None:
        for t in bucket:
            if str(t.get("trace_id")) == str(trace_id) and q in _blob(t):
                return True
    return any(q in _blob(t) for t in bucket)


def reconcile(comp, facts, bucket, floor=3):
    """The analyst read + deterministic guard flags. Never rates; only surfaces truth-checks against the facts."""
    ev = comp.get("evidence") or {}
    citation_ok = _quote_exists(ev.get("quote"), bucket, ev.get("trace_id"))
    n = facts.get("n", len(bucket))
    coh = (comp.get("coherence") or {}).get("verdict")
    flags = []
    if (facts.get("distinct_system") or 1) > 1:                       # the frame is NOT fixed — surface it always
        flags.append("system varies across calls (%d distinct) — dynamic frame, verify one operation"
                     % facts["distinct_system"])
    if facts.get("mixed_shapes"):                                     # DETERMINISTIC over ALL N — catches rare modes
        flags.append("outputs span multiple shapes over all %d calls (%s) — possible MIXED bucket"
                     % (n, ", ".join("%s×%d" % (k, v) for k, v in (facts.get("output_shapes") or {}).items())))
    if coh == "possible_mixed_bucket":                                # keep the analyst's mixed flag visible too
        flags.append("analyst also flags possible MIXED bucket")
    if not citation_ok:                                               # ungrounded claim
        flags.append("citation not found in traces — claim treated as ungrounded")
    if n < floor:                                                     # thin evidence
        flags.append("thin evidence (%d samples) — described, not asserted" % n)
    return {"op": comp.get("op"), "summary": comp.get("summary"), "coherence": coh,
            "citation_ok": citation_ok, "flags": flags, "evidence": ev}
