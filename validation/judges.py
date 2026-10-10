"""The two LLM judges, and the strict readers of their replies.

fit        does ONE candidate output belong to the reference set: equivalent, a variation the original allows, or
           drift? Majority of `votes` calls decides (engine); an unsure vote abstains.
coherence  how many of the reference outputs agree with each other — the bar a candidate is held to.

A verdict is the first line that STARTS with the verdict word, case-sensitive; prose that merely contains one is not a
verdict. A reply with no readable verdict is never read as a pass: fit -> an abstaining vote, coherence -> unknown.
"""
import re

from validation.pricing import cost_of

FIT_SYSTEM = (
    "You compare a CANDIDATE output with a REFERENCE SET: outputs the same model produced for the SAME request, all "
    "acceptable. Decide whether the candidate behaves the way the reference set does.\n"
    "Read the reference set as evidence of what the model keeps FIXED and what it treats as FREE:\n"
    "- FIXED: what every reference agrees on: decisions, classifications, chosen actions and tool calls with their "
    "material arguments, values taken from the request or a tool result, required parts and the arguments a tool "
    "definition requires, format, refusals.\n"
    "- FREE: where the references differ from one another: wording, order, length, level of detail, which of several "
    "valid options or examples is chosen.\n"
    "- In JSON or tool arguments, an atomic value (a label, status, code, id, number, date, flag) is content; a "
    "free-text field written for a person (a reply, message, summary, explanation) is read like prose: its wording is "
    "free, its facts are not.\n"
    "Classify the candidate:\n"
    "EQUIVALENT - the same fixed content; it differs only in wording, order or form.\n"
    "VARIATION - keeps all fixed content and differs in a free aspect with a choice the request allows; an option or "
    "example no reference used is fine when it is valid for the request. It may add general, correct context that "
    "states nothing specific about the request's people, organisation, policies, products, numbers or systems. A "
    "candidate that leaves out a detail every reference gives (a date, amount, name or step) is never EQUIVALENT: it "
    "is VARIATION when whoever uses it still has everything they need, DRIFT when they do not.\n"
    "DRIFT - any of: changes or drops fixed content (another decision, label, action, tool or material argument; a "
    "changed value from the request or a tool result, even one that looks like a typo; a part every reference has; the format every reference uses); states a specific "
    "claim that neither the request nor the references support (about a person, customer, organisation, policy, "
    "product, number, or what a system does or stores), even if it sounds plausible; makes a commitment or takes an "
    "action the references do not; breaks an instruction in the request.\n"
    "UNSURE - what you were given is not enough to tell.\n"
    "Reply with ONE line only: the category, then ' - ', then a <=15-word reason naming the specific fixed content "
    "kept, or the specific difference. Examples:\n"
    "EQUIVALENT - same decision and identifier; wording differs\n"
    "VARIATION - chooses another option the request allows; all fixed content kept\n"
    "DRIFT - claims a system behaviour that neither the request nor the references state"
)
# request + reference set are identical across every fit call for one item -> the cached prefix; only the candidate varies
FIT_PREFIX = "REQUEST:\n%s\n\nREFERENCE SET — %d outputs the model considers ACCEPTABLE for this request:\n%s\n\n"

COHERENCE_SYSTEM = "You assess how consistent several outputs from the SAME model are for the SAME request."
COHERENCE_TEMPLATE = (
    "REQUEST:\n%s\n\n"
    "Here are %d outputs the SAME model produced for this request:\n%s\n\n"
    "How many carry the SAME decision / classification / required facts / values / tool call as one another? Judge "
    "material CONTENT, not surface form: wording, ordering and format differences do NOT count as disagreement UNLESS "
    "they change the meaning; a changed decision or a different material value DOES. Where the request leaves room "
    "for several valid answers (which option, example or detail to give), outputs that choose differently but are "
    "each valid AGREE; only a contradiction, or a different decision or value the request fixes, is divergence.\n"
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


_CATEGORY = {"EQUIVALENT": True, "VARIATION": True, "KEPT": True, "DRIFT": False, "BROKE": False, "UNSURE": None}


def parse_fit(raw):
    """-> (kept, reason). kept: True (equivalent, or a variation the original allows), False (drift), or None when the
    judge is unsure or its reply has no verdict: an abstaining vote, never read as a pass or a fail. A variation's or
    an unsure vote's reason says so in the words users see ("acceptable: ...", "unclear: ...")."""
    m = re.search(r"^\W*(?:[Vv]erdict\W*)?(EQUIVALENT|VARIATION|DRIFT|UNSURE|KEPT|BROKE)\b", raw or "", flags=re.M)
    if not m:
        return None, "no verdict in the judge's reply"
    word = m.group(1)
    kept = _CATEGORY[word]
    tail = raw[m.end():].split("\n", 1)[0].strip(" \t:.-–—*_`|")
    reason = tail[:240] or {True: "matches the set", False: "differs from the set", None: "the judge is unsure"}[kept]
    shown = {"VARIATION": "acceptable", "UNSURE": "unclear"}.get(word)
    return kept, ("%s: %s" % (shown, reason) if shown else reason)


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
    text = llm_client.text(r).strip()
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
