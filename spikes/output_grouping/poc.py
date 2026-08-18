"""PoC — does STRATIFY-BY-(output decision × structural shape) recover a call-site's input KINDS, per call-site?

CONCEPT validation only (no traces). For four realistic call-sites we plant records = (input, output, TRUE_KIND),
with heavy input+output variation, then partition each call-site by a DETERMINISTIC key computed from the
recorded OUTPUT (the node's own decision) + the input's STRUCTURAL SHAPE — NO embeddings, NO LLM. We measure:

  - homogeneity / completeness / V-measure of (partition vs true kind)
  - COVERAGE@1: sample 1 record per partition -> how many TRUE kinds get a witness?  (missed kind = false-SAFE risk)

Deliberately includes the two failure modes so the boundary is honest:
  C = free-form SUMMARIZER (no decision in the output) — the degenerate case
  D = binary CLASSIFIER whose coarse label HIDES sub-behaviors — the under-split case

Run: python -m spikes.output_grouping.poc   (from repo root)
"""
import sys
import json
import random

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
random.seed(7)

from sklearn.metrics import homogeneity_completeness_v_measure


# ─────────────────────────────────────────────────────────────────────────────
# DETERMINISTIC signals (this is the whole method — no embeddings, no LLM)
# ─────────────────────────────────────────────────────────────────────────────
def _len_band(n):
    return "xs" if n < 200 else "s" if n < 1000 else "m" if n < 4000 else "l"


def structural_shape(rec):
    """Deterministic features of the request envelope: role-shape, tool-result present, RAG present, length band."""
    msgs = rec["input_messages"]
    roles = tuple(m["role"] for m in msgs)
    has_tool = any(m["role"] == "tool" for m in msgs)
    has_rag = any("<retrieved>" in (m.get("content") or "") for m in msgs)
    total = sum(len(m.get("content") or "") for m in msgs)
    return (roles, has_tool, has_rag, _len_band(total))


def output_decision(rec):
    """The node's OWN emitted decision, parsed deterministically from the recorded output:
      TOOL:name(...)  -> ('tool', name)      · a JSON object -> ('json', sorted keys)
      a short token   -> ('label', token)    · anything else -> ('prose',)  <-- NO decision signal
    'prose' is the honest degenerate: a free-form generator commits to no discrete decision."""
    out = (rec["output"] or "").strip()
    if out.startswith("TOOL:"):
        return ("tool", out[5:].split("(", 1)[0].strip())
    if out[:1] in "{[":
        try:
            obj = json.loads(out)
            return ("json", tuple(sorted(obj.keys())) if isinstance(obj, dict) else ("array",))
        except Exception:
            pass
    if len(out.split()) <= 3 and "\n" not in out:                 # a short classifier/router token
        return ("label", out.lower())
    return ("prose",)


def partition_key(rec):
    return (structural_shape(rec), output_decision(rec))


# ─────────────────────────────────────────────────────────────────────────────
# Four realistic call-sites with PLANTED ground truth + heavy variation
# ─────────────────────────────────────────────────────────────────────────────
def _u(text):
    return [{"role": "system", "content": "You are the node."}, {"role": "user", "content": text}]


def call_site_A_router():
    """ROUTER — output is one of N route labels. Inputs (tickets) vary wildly. TRUE kind = the route."""
    routes = {
        "billing":   ["charged twice for {o}", "refund not received on {o}", "invoice wrong for {o}",
                      "double billing {o}", "why was I charged {o}"],
        "security":  ["account {o} hacked", "suspicious login {o}", "reset MFA for {o}", "data breach {o}?"],
        "access":    ["cannot log in {o}", "need admin role {o}", "locked out of {o}", "grant seat {o}"],
        "provision": ["cluster {o} won't start", "region {o} unavailable", "quota exceeded {o}"],
        "integrat":  ["webhook {o} failing", "API 500 on {o}", "SDK {o} broken"],
    }
    recs = []
    for route, tmpls in routes.items():
        for i in range(14):
            t = random.choice(tmpls).format(o="order-%d" % random.randint(100, 999))
            recs.append({"input_messages": _u(t + " " + "detail " * random.randint(0, 40)),
                         "output": route, "true_kind": route})
    return recs


def call_site_B_tool_worker():
    """TOOL-CALLER — calls lookup_account / issue_refund / answers directly; sometimes a post-tool turn.
    TRUE kind = (phase, tool). A clean decision node."""
    recs = []
    for i in range(16):
        recs.append({"input_messages": _u("look up account for order-%d" % random.randint(1, 999)),
                     "output": "TOOL:lookup_account(id=%d)" % random.randint(1, 999), "true_kind": "init/lookup"})
    for i in range(12):
        recs.append({"input_messages": _u("please refund order-%d amount %d" % (random.randint(1, 999), random.randint(5, 99))),
                     "output": "TOOL:issue_refund(id=%d)" % random.randint(1, 999), "true_kind": "init/refund"})
    for i in range(10):
        recs.append({"input_messages": _u("what are your hours? %s" % ("x" * random.randint(0, 50))),
                     "output": "We are open 24/7 and happy to help with your request.", "true_kind": "init/answer"})
    for i in range(12):
        recs.append({"input_messages": [{"role": "system", "content": "You are the node."},
                                        {"role": "user", "content": "resolve order-%d" % random.randint(1, 999)},
                                        {"role": "tool", "content": "account: 2 charges of $20"}],
                     "output": "I've refunded the duplicate $20 charge; you'll see it in 3-5 days.",
                     "true_kind": "after-tool/final"})
    return recs


def call_site_C_summarizer():
    """FREE-FORM SUMMARIZER — output is ALWAYS prose (no decision). Inputs = 4 document domains. TRUE kind = domain.
    TWO domains are structurally distinct (legal=very long, chat=many turns); TWO are structurally SIMILAR
    (policy vs faq, both medium prose) — the purely-semantic residual the method CANNOT split."""
    recs = []
    # legal: very long single doc  (structurally distinct -> length band 'l')
    for i in range(12):
        recs.append({"input_messages": _u("Summarize this contract:\n" + "WHEREAS clause %d. " % i * 400),
                     "output": "The contract sets obligations, termination terms, and liability caps.",
                     "true_kind": "legal"})
    # chat log: many turns  (structurally distinct -> role-shape has many user turns)
    for i in range(12):
        msgs = [{"role": "system", "content": "You are the node."}]
        for t in range(random.randint(6, 10)):
            msgs.append({"role": "user", "content": "chat turn %d about issue" % t})
        recs.append({"input_messages": msgs, "output": "The user reported a login issue; agent reset the password.",
                     "true_kind": "chatlog"})
    # policy doc: medium prose  (structurally like faq)
    for i in range(12):
        recs.append({"input_messages": _u("Summarize this policy:\n" + "Refunds within 30 days. " * 30),
                     "output": "Refunds are allowed within 30 days of purchase under stated conditions.",
                     "true_kind": "policy"})
    # faq page: medium prose  (structurally like policy -> NOT separable by shape or output)
    for i in range(12):
        recs.append({"input_messages": _u("Summarize this FAQ:\n" + "Q: how to reset? A: click reset. " * 28),
                     "output": "The FAQ explains password reset, billing, and contact options.",
                     "true_kind": "faq"})
    return recs


def call_site_D_classifier():
    """BINARY CLASSIFIER — output in {escalate, resolve}. But each label hides a trivial and a subtle sub-behavior
    (where a cheaper model is most likely to FLIP). TRUE kind has 4 classes; the coarse label gives only 2."""
    recs = []
    for i in range(14):
        recs.append({"input_messages": _u("P1 outage region down for %d hours" % random.randint(2, 9)),
                     "output": "escalate", "true_kind": "clear_escalate"})
    for i in range(12):     # subtle escalate — reads calm but IS urgent (downgrade-fragile)
        recs.append({"input_messages": _u("minor note: a few users mention slow logins since the migration"),
                     "output": "escalate", "true_kind": "subtle_escalate"})
    for i in range(14):
        recs.append({"input_messages": _u("how do I change my avatar? %s" % ("x" * random.randint(0, 30))),
                     "output": "resolve", "true_kind": "trivial_resolve"})
    for i in range(12):     # subtle resolve — reads scary but is benign (downgrade-fragile)
        recs.append({"input_messages": _u("URGENT!!! my dashboard color looks wrong after the update"),
                     "output": "resolve", "true_kind": "subtle_resolve"})
    return recs


# ─────────────────────────────────────────────────────────────────────────────
# Evaluate one call-site
# ─────────────────────────────────────────────────────────────────────────────
def evaluate(name, recs):
    keys = [partition_key(r) for r in recs]
    truth = [r["true_kind"] for r in recs]
    kmap = {k: i for i, k in enumerate(dict.fromkeys(keys))}
    tmap = {t: i for i, t in enumerate(dict.fromkeys(truth))}
    kids = [kmap[k] for k in keys]
    tids = [tmap[t] for t in truth]
    h, c, v = homogeneity_completeness_v_measure(tids, kids)

    # COVERAGE@1: one representative per partition -> which true kinds get a witness?
    seen_reps = {}
    for r, k in zip(recs, keys):
        seen_reps.setdefault(k, r["true_kind"])       # first record of each partition = its representative
    witnessed = set(seen_reps.values())
    missed = [t for t in tmap if t not in witnessed]
    cov = len(witnessed) / len(tmap)

    verdict = ("WORKS" if v > 0.85 and not missed else
               "PARTIAL (structure helps, semantic residual left)" if v > 0.45 else
               "FAILS (needs the advisory facet/output-cluster layer)")
    print("── %s" % name)
    print("   true kinds: %d   partitions found: %d" % (len(tmap), len(kmap)))
    print("   homogeneity %.2f  completeness %.2f  V-measure %.2f" % (h, c, v))
    print("   coverage@1 (kinds witnessed by 1-per-partition): %d/%d = %.0f%%   MISSED: %s"
          % (len(witnessed), len(tmap), cov * 100, missed or "none"))
    print("   VERDICT: %s\n" % verdict)
    return v, cov, missed


if __name__ == "__main__":
    print("PoC: stratify a call-site by (output decision x structural shape) — deterministic, $0, no embeddings\n")
    evaluate("A. ROUTER (decision node)", call_site_A_router())
    evaluate("B. TOOL-CALLER (decision node)", call_site_B_tool_worker())
    evaluate("C. FREE-FORM SUMMARIZER (no decision in output)", call_site_C_summarizer())
    evaluate("D. BINARY CLASSIFIER (coarse label hides sub-behavior)", call_site_D_classifier())
    print("Read: WORKS where the node commits to a decision (A,B). PARTIAL/FAILS where the output is free-form (C)")
    print("or the label is coarser than the behavior (D) — exactly the residual for the bounded advisory layer.")
