"""Synthetic, ground-truth-labelled call-site inputs to VALIDATE the grouping concept.

Why this exists: the whole profiling plan rests on one unproven assumption — that
`frame-strip -> semantic-embed -> cluster` recovers a node's true INPUT-KINDS even when inputs vary
wildly in shape, length, and complexity, with failure/noise cases mixed in. We plant the kinds so
recovery is MEASURABLE (v-measure / ARI vs the planted labels), not a vibe.

Design: rich per-kind paraphrase pools + varied fillers (genuine within-kind variety, no LLM), plus
the deliberately HARD cases that break naive approaches:
  - dynamic_router : system frame VARIES per call (queue state); the kind is in the user turn.
  - rag_answer     : a short question buried in ~10k tokens of retrieved docs (kind = the question).
  - planner        : 3 task kinds, wide length/phrasing variety.
  - doc_summarizer : ONE kind ('summarize'), but payloads span legal/medical/code/news (does the
                     method group by WHAT'S ASKED, not by payload topic?).
  - mixed_bucket   : one node accidentally doing TWO unrelated jobs (must show up as 2 kinds).
  - noise          : junk/empty/malformed woven in (must land in the HDBSCAN noise bucket).
Each record also carries near-duplicate retries (to exercise MinHash dedup) and length/shape variance.

Deterministic (fixed rng seed) so the test is reproducible.
"""
from __future__ import annotations
import json, random

RNG = random.Random(20260816)

# ─────────────────────────────────────────────────────────────────────────────
# filler generators (varied realistic-ish payloads, no LLM, deterministic)
# ─────────────────────────────────────────────────────────────────────────────
_LOREM_DOMAINS = {
    "legal": ("The parties hereto agree that indemnification shall survive termination. "
              "Notwithstanding the foregoing, liability under Section {n} is capped at the fees paid. "
              "Governing law is the State of Delaware; venue lies exclusively in New Castle County. "),
    "medical": ("Patient presents with intermittent tachycardia and a HR of {n} bpm at rest. "
                "Differential includes SVT versus sinus tachycardia secondary to anxiety. "
                "Recommend a 12-lead ECG, TSH panel, and a 48-hour Holter monitor. "),
    "code": ("def process(batch: list[int]) -> int:\n    total = 0\n    for x in batch:  # item {n}\n"
             "        if x % 2 == 0:\n            total += x * x\n    return total\n"
             "# NOTE: refactor to a comprehension once the retry path lands\n"),
    "news": ("Markets closed mixed on Tuesday as the index shed {n} points amid rate-cut uncertainty. "
             "Energy led decliners while small-caps rallied into the afternoon session. "
             "Analysts cited thin volume ahead of the holiday-shortened week. "),
}

def _doc(domain: str, approx_tokens: int) -> str:
    """Build a varied ~approx_tokens document from a domain seed (repetition with varied numbers)."""
    seed = _LOREM_DOMAINS[domain]
    out, n_words = [], 0
    while n_words < approx_tokens:  # ~1 token ≈ 0.75 words; close enough for a spike
        chunk = seed.format(n=RNG.randint(2, 999))
        out.append(chunk)
        n_words += len(chunk.split())
    RNG.shuffle(out)
    return "".join(out)

def _pick(pool: list[str], **slots) -> str:
    return RNG.choice(pool).format(**slots)

# ─────────────────────────────────────────────────────────────────────────────
# per-kind paraphrase pools (genuine lexical variety within a kind)
# ─────────────────────────────────────────────────────────────────────────────
ROUTER_KINDS = {
    "billing": [
        "I was double charged for order {oid}, can you refund the extra?",
        "Why is there a ${amt} charge I don't recognize on my card?",
        "My invoice total looks wrong — it says ${amt} but I paid less.",
        "Please cancel the subscription and refund this month's ${amt} fee.",
        "I need a receipt for payment {oid}, the amount was ${amt}.",
        "You charged me twice. Order {oid}. Fix the billing please.",
        "Requesting a chargeback reversal on the ${amt} transaction.",
        "The promo code didn't apply and I got billed full price ${amt}.",
    ],
    "technical": [
        "The app crashes on launch after the latest update, build {oid}.",
        "I'm getting a 500 error every time I hit the export button.",
        "Sync has been broken for two days, nothing updates across devices.",
        "The dashboard won't load — just an infinite spinner in Chrome.",
        "API returns timeout on /reports, correlation id {oid}.",
        "Push notifications stopped working entirely since Tuesday.",
        "Uploads fail with 'file too large' even for a 2MB image.",
        "The mobile client freezes when I open the settings screen.",
    ],
    "account_access": [
        "I can't log in, the password reset email never arrives.",
        "My account got locked after a few tries, please unlock it.",
        "I lost access to my 2FA device and can't sign in.",
        "How do I change the email address on my account {oid}?",
        "Someone may have accessed my account — I see logins I didn't make.",
        "Reset my password, I've been locked out since this morning.",
        "The magic link keeps saying expired even right after I request it.",
        "Need to transfer ownership of the workspace to a new admin.",
    ],
}

PLANNER_KINDS = {
    "research_task": [
        "Research the top 5 competitors in the EV charging space and summarize positioning.",
        "Find recent papers on retrieval-augmented generation and note key benchmarks.",
        "Investigate why our churn spiked in Q2 and gather supporting data.",
        "Look into GDPR requirements for storing biometric data in the EU.",
        "Survey the market for open-source vector databases and compare licensing.",
    ],
    "coding_task": [
        "Implement a rate limiter middleware with a token-bucket algorithm.",
        "Refactor the auth module to use dependency injection and add tests.",
        "Write a script to migrate the users table to the new schema.",
        "Add pagination to the /orders endpoint and cache the hot queries.",
        "Fix the race condition in the background worker's retry loop.",
    ],
    "writing_task": [
        "Draft a launch announcement blog post for the new analytics feature.",
        "Write release notes for v2.3 aimed at non-technical customers.",
        "Compose a polite follow-up email to a lead who went quiet.",
        "Turn these bullet points into an executive summary for the board.",
        "Write onboarding documentation for the new billing workflow.",
    ],
}

RAG_QUESTIONS = {
    "factual_lookup": [
        "What is the maximum liability cap stated in the contract?",
        "What HR value did the patient present with at rest?",
        "How many points did the index shed on Tuesday?",
        "What governing law applies under this agreement?",
        "What return code does the /reports endpoint throw?",
    ],
    "summarize_request": [
        "Summarize the key obligations of each party in two sentences.",
        "Give me a short recap of the patient's differential diagnosis.",
        "TL;DR the market movement described above.",
        "Briefly summarize what this function does.",
        "Condense the recommended next steps into a bullet list.",
    ],
    "comparison": [
        "Compare the liability terms here against a standard MSA.",
        "How does this treatment plan differ from the prior visit?",
        "Contrast today's market action with last week's.",
        "Compare this implementation to a comprehension-based one.",
        "How does this clause compare to the indemnification section?",
    ],
}

SUMMARIZER_INSTRUCTIONS = [  # ONE kind ('summarize'), payloads vary across domains (below)
    "Summarize the following document.",
    "Give me a concise summary of the text below.",
    "Provide a short summary of this.",
    "Summarize the key points.",
    "TL;DR of the document that follows.",
]

MIXED_A = [  # job 1: sentiment
    "Classify the sentiment of this review: '{payload}'",
    "Is the following review positive or negative? '{payload}'",
    "Rate the sentiment (pos/neg/neutral): '{payload}'",
]
MIXED_B = [  # job 2: invoice extraction — genuinely unrelated
    "Extract the invoice total from this text: '{payload}'",
    "What is the amount due in the following invoice? '{payload}'",
    "Pull out the grand total from this billing text: '{payload}'",
]
REVIEWS = ["Loved it, works great and fast shipping", "Terrible, broke after a day",
           "It's fine, nothing special", "Absolutely fantastic support team", "Would not buy again"]
INVOICES = ["Invoice #{n}\nSubtotal $120\nTax $10\nTotal $130",
            "Billing summary — amount due: $89.50 by net-30",
            "Grand Total: $1,240.00 (PO 55{n})"]

NOISE = [
    "", "   ", "asdkjfh q3;lk4j ;lkj23", "{{{ broken json ][", "\x00\x01\x02 garbage",
    "null", "undefined", "😀😀😀😀", "................", "test test test 123",
]

# ─────────────────────────────────────────────────────────────────────────────
# record builders — each returns messages[] (structured, roles) + planted kind
# ─────────────────────────────────────────────────────────────────────────────
STATIC_SUPPORT_SYS = ("You are a support triage assistant. Read the customer's message and route it. "
                      "Always be concise. Never make promises about refunds. Follow policy 4.2.")

def _msgs(system: str, user: str):
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]

def _dup_variants(text: str, k: int):
    """Return k near-duplicate variants (retries) — tiny surface changes, same meaning."""
    outs = [text]
    for _ in range(k - 1):
        t = text
        if RNG.random() < 0.5: t = t + " "  # trailing space
        if RNG.random() < 0.5: t = t.replace("please", "pls")
        if RNG.random() < 0.3: t = t + " Thanks."
        outs.append(t)
    return outs

def build_dataset():
    records = []  # each: {node, kind, messages}

    # 1) dynamic_router: system frame VARIES per call (queue state); kind is in the USER turn
    for kind, pool in ROUTER_KINDS.items():
        for _ in range(40):
            dyn_sys = (f"[queue depth: {RNG.randint(3,900)}] [agent: {RNG.choice(['A','B','C'])}] "
                       f"[recent tickets: {RNG.randint(0,50)}]\n" + STATIC_SUPPORT_SYS)
            user = _pick(pool, oid=f"ORD-{RNG.randint(1000,9999)}", amt=RNG.randint(5, 400))
            records.append({"node": "dynamic_router", "kind": kind, "messages": _msgs(dyn_sys, user)})
    # add near-duplicate retries (dedup target)
    for _ in range(12):
        kind = RNG.choice(list(ROUTER_KINDS))
        base = _pick(ROUTER_KINDS[kind], oid="ORD-5555", amt=99)
        for v in _dup_variants(base, 3):
            dyn_sys = f"[queue depth: {RNG.randint(3,900)}]\n" + STATIC_SUPPORT_SYS
            records.append({"node": "dynamic_router", "kind": kind, "messages": _msgs(dyn_sys, v)})

    # 2) rag_answer: short question buried in ~10k tokens of docs (kind = the question intent)
    rag_sys = "Answer the question using ONLY the provided context. Cite nothing outside it."
    domains = list(_LOREM_DOMAINS)
    for kind, pool in RAG_QUESTIONS.items():
        for _ in range(40):
            doc = _doc(RNG.choice(domains), approx_tokens=RNG.choice([1500, 4000, 8000]))
            q = RNG.choice(pool)
            user = f"Context:\n{doc}\n\nQuestion: {q}"
            records.append({"node": "rag_answer", "kind": kind, "messages": _msgs(rag_sys, user)})

    # 3) planner: 3 task kinds, wide phrasing/length variety
    plan_sys = "You are a planning agent. Decompose the task into steps."
    for kind, pool in PLANNER_KINDS.items():
        for _ in range(40):
            task = RNG.choice(pool)
            if RNG.random() < 0.4:  # length variance
                task += " " + RNG.choice(pool)
            records.append({"node": "planner", "kind": kind, "messages": _msgs(plan_sys, task)})

    # 4) doc_summarizer: ONE kind ('summarize'), payloads span domains/lengths/shapes
    sum_sys = "You summarize documents faithfully and briefly."
    for _ in range(120):
        instr = RNG.choice(SUMMARIZER_INSTRUCTIONS)
        doc = _doc(RNG.choice(domains), approx_tokens=RNG.choice([200, 1500, 6000]))
        shape = RNG.random()
        if shape < 0.4:
            user = f"{instr}\n\n{doc}"
        elif shape < 0.7:
            user = json.dumps({"instruction": instr, "document": doc})          # JSON shape
        else:
            user = f"<<TOOL_RESULT>>\n{doc}\n<<END>>\n\n{instr}"                  # tool-wrapped shape
        records.append({"node": "doc_summarizer", "kind": "summarize", "messages": _msgs(sum_sys, user)})

    # 5) mixed_bucket: one node doing TWO unrelated jobs (must recover 2 kinds)
    mix_sys = "You are a text-processing utility."
    for _ in range(40):
        user = _pick(MIXED_A, payload=RNG.choice(REVIEWS))
        records.append({"node": "mixed_bucket", "kind": "sentiment", "messages": _msgs(mix_sys, user)})
    for _ in range(40):
        user = _pick(MIXED_B, payload=RNG.choice(INVOICES).format(n=RNG.randint(10,99)))
        records.append({"node": "mixed_bucket", "kind": "invoice_extract", "messages": _msgs(mix_sys, user)})

    # 6) noise woven into a couple of nodes (must land in HDBSCAN noise bucket)
    for node in ("dynamic_router", "mixed_bucket", "planner"):
        for _ in range(6):
            records.append({"node": node, "kind": "noise",
                            "messages": _msgs("sys", RNG.choice(NOISE))})

    RNG.shuffle(records)
    return records

def summary(records):
    from collections import Counter
    by_node = {}
    for r in records:
        by_node.setdefault(r["node"], Counter())[r["kind"]] += 1
    return by_node

if __name__ == "__main__":
    recs = build_dataset()
    print(f"total records: {len(recs)}\n")
    for node, kinds in summary(recs).items():
        planted = {k: v for k, v in kinds.items() if k != "noise"}
        print(f"{node:16} planted-kinds={len(planted):>2}  "
              f"counts={dict(kinds)}")
