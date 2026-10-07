"""The two LLM judges, and the strict readers of their replies.

fit        does ONE candidate output belong to the reference set? Majority of `votes` calls decides (engine).
coherence  how many of the reference outputs agree with each other — the bar a candidate is held to.

A verdict is the first line that STARTS with the verdict word, case-sensitive; prose that merely contains "kept" or
"broke" is not a verdict. A reply with no readable verdict is never read as a pass: fit -> BROKE, coherence -> unknown.
"""
import re

from validation.pricing import cost_of

FIT_SYSTEM = (
    "You decide whether a CANDIDATE output belongs to the same behaviour as a REFERENCE SET of outputs the model "
    "already considers acceptable for the SAME request. Does the CANDIDATE carry the SAME decision / classification "
    "/ required facts / values / tool call — SAME tool AND same material arguments — as the set, differing ONLY in "
    "ways the set ITSELF already shows? Judge material CONTENT, not surface form:\n"
    "- Wording and ordering differences are always fine.\n"
    "- A FORMAT or STRUCTURE difference (e.g. JSON vs plain text) is fine ONLY IF the reference set itself varies "
    "that way. If every output in the set holds one form, a candidate that breaks it does NOT belong.\n"
    "- A changed decision, a changed tool argument, a dropped / added material fact or value, or an invented / "
    "contradicted claim NEVER belongs.\n"
    "- If unsure -> BROKE.\n"
    "Reply with ONE line only: the word KEPT or BROKE, then ' - ', then a <=15-word reason naming the specific "
    "decision / fact / value / tool-argument that matches or differs. Examples:\n"
    "KEPT - same escalate decision and same account_id argument\n"
    "BROKE - changed the refund amount from 50 to 100"
)
# request + reference set are identical across every fit call for one item -> the cached prefix; only the candidate varies
FIT_PREFIX = "REQUEST:\n%s\n\nREFERENCE SET — %d outputs the model considers ACCEPTABLE for this request:\n%s\n\n"

COHERENCE_SYSTEM = "You assess how consistent several outputs from the SAME model are for the SAME request."
COHERENCE_TEMPLATE = (
    "REQUEST:\n%s\n\n"
    "Here are %d outputs the SAME model produced for this request:\n%s\n\n"
    "How many carry the SAME decision / classification / required facts / values / tool call as one another? Judge "
    "material CONTENT, not surface form — wording, ordering, and format differences do NOT count as disagreement "
    "UNLESS they change the meaning; a changed decision or a different material value DOES.\n"
    "Line 1 — output exactly: CONSISTENT n/%d\n"
    "Line 2 — output: DIVERGENT followed by the numbers of the outputs that break from the majority "
    "(e.g. 'DIVERGENT 2,4'), or 'DIVERGENT none'.\n"
    "Then ONE line per divergent number: '<number>: <=12-word reason it differs from the majority'."
)


def cap(s, n):
    """Cut for a judge prompt, never silently: the judge is told its view is partial (and the engine marks the item
    unverified rather than certifying on a clipped view)."""
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + "\n…[TRUNCATED %d chars — output exceeds the judge's %dk-char budget]" % (len(s) - n, n // 1000)


def parse_fit(raw):
    """-> (kept, reason). No verdict line -> BROKE ("if unsure -> BROKE"), and the reason says the reply was unreadable."""
    m = re.search(r"^\W*(?:[Vv]erdict\W*)?(KEPT|BROKE)\b", raw or "", flags=re.M)
    if not m:
        return False, "no verdict in the judge's reply"
    kept = m.group(1) == "KEPT"
    tail = raw[m.end():].split("\n", 1)[0].strip(" \t:.-–—*_`|")
    return kept, (tail[:240] or ("matches the set" if kept else "differs from the set"))


def parse_coherence(raw, total):
    """-> (agree_count, divergent set, {number: reason}) or None when the reply carries no readable count."""
    raw = raw or ""
    reasons = {int(m.group(1)): m.group(2).strip()[:120]
               for m in re.finditer(r"^\s*(\d+)\s*[:\-]\s*(.+)$", raw, flags=re.M) if 1 <= int(m.group(1)) <= total}
    dm = re.search(r"^\W*DIVERGENT\b[:\s]*(none|[0-9, ]+)", raw, flags=re.M | re.I)     # the list stays on its line
    if dm:
        div = set() if dm.group(1).strip().lower() == "none" else \
            {d for d in (int(x) for x in re.findall(r"\d+", dm.group(1))) if 1 <= d <= total}
        return total - len(div), div, reasons
    cm = re.search(r"CONSISTENT\W*(\d+)\s*/\s*\d+", raw, flags=re.I)
    if cm:
        return max(0, min(int(cm.group(1)), total)), set(), reasons
    return None


def _complete(spec, system, messages):
    from app.services import llm_client
    r = llm_client.complete(model=spec.judge_model, max_tokens=spec.judge_max_tokens, system=system, messages=messages)
    text = "".join(getattr(x, "text", "") or "" for x in (getattr(r, "content", None) or [])).strip()
    return text, (cost_of(spec.judge_model, llm_client.usage(r)) or 0.0)


def fit(reference, candidate, request, spec):
    """-> (kept, reason, judge_cost_usd): does `candidate` belong to the `reference` outputs for `request`?"""
    from app.services import llm_client
    ref = "\n\n".join("--- acceptable %d ---\n%s" % (i + 1, cap(o, spec.ref_chars)) for i, o in enumerate(reference))
    prefix = FIT_PREFIX % (request[:spec.cap_chars], len(reference), ref)
    raw, cost = _complete(spec, FIT_SYSTEM, [{"role": "user", "content": [
        llm_client.cache_block(prefix), {"type": "text", "text": "CANDIDATE:\n%s" % cap(candidate, spec.cap_chars)}]}])
    kept, reason = parse_fit(raw)
    from app.services import debugcap                           # AUDIT_DEBUG: keep the raw reply so it can be inspected
    if debugcap.enabled():
        user = request.split("USER:")[-1] if "USER:" in request else request
        debugcap.record_judge({"model": spec.judge_model, "raw": raw[:800], "kept": kept, "reason": reason,
                               "input": user.strip()[:300], "candidate": (candidate or "").strip()[:400]})
    return kept, reason, cost


def coherence(request, outputs, spec):
    """-> {agree, of, divergent, reasons, cost_usd}; `agree` is None when the reply could not be read."""
    ref = "\n\n".join("--- output %d ---\n%s" % (i + 1, cap(o, spec.ref_chars)) for i, o in enumerate(outputs))
    total = len(outputs)
    raw, cost = _complete(spec, COHERENCE_SYSTEM,
                          [{"role": "user", "content": COHERENCE_TEMPLATE % (request[:spec.cap_chars], total, ref, total)}])
    parsed = parse_coherence(raw, total)
    if parsed is None:
        return {"agree": None, "of": total, "divergent": set(), "reasons": {}, "cost_usd": cost}
    agree, divergent, reasons = parsed
    return {"agree": agree, "of": total, "divergent": divergent, "reasons": reasons, "cost_usd": cost}
