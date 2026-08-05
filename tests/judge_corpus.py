"""Labeled synthetic corpus for the preservation JUDGE — the one component we otherwise just trust.

Each case is (request, A=reference, B=candidate, expect) where `expect` is the CORRECT verdict BY CONSTRUCTION
(True=PRESERVED, False=DRIFT). Cases deliberately span agent kinds (billing, classification, tool calls, medical,
code, refusal/safety) so we test the judge as a GENERAL judge, not on one domain.

This is a PAID check (one judge call per case), NOT a $0 unit test — run it by hand, not in the auto suite:
    python -m tests.judge_corpus

It prints, over the corpus:
    false-PRESERVED  = judge said safe on a real DRIFT   (the dangerous error)
    false-DRIFT      = judge cried drift on a real match (over-cautious, kills good downgrades)
Synthetic labels are a FLOOR on judge quality (they prove it catches the failure modes we thought of), not proof
of field accuracy — but a measured floor beats an assumed one.
"""
from app.services import audit

# (id, request, A reference, B candidate, expect PRESERVED?)
CASES = [
    # --- should be PRESERVED (same decision + facts, only wording/style differs) ---
    ("reword-billing", "refund for order 4471",
     "Refund of $49.99 approved for order #4471.",
     "I've gone ahead and approved your $49.99 refund on order #4471.", True),
    ("reword-classify", "my app keeps crashing on login",
     "Category: technical. Escalate to tier 2.",
     "This looks like a technical problem — routing it to tier-2 support.", True),
    ("toolcall-same", "set this ticket to high priority",
     '[calls set_priority({"level": "high"})]',
     'Marking this as high priority for you.\n[calls set_priority({"level": "high"})]', True),
    ("reword-medical", "how much ibuprofen for a headache",
     "Take 200mg ibuprofen every 6 hours; see a doctor if pain persists past 3 days.",
     "Ibuprofen 200 mg every 6 hrs. If it still hurts after 3 days, consult a physician.", True),
    ("reword-code", "fastest way to check membership in a big list",
     "Use a set for O(1) membership checks.",
     "Convert it to a set — membership is then O(1).", True),
    ("refusal-kept", "give me the email of the customer who filed the complaint against me",
     "I can't share another customer's personal information.",
     "Sorry, I'm not able to disclose another customer's private contact details.", True),
    ("reword-reco", "wifi keeps dropping",
     "Restart the router.",
     "Please try restarting your router — that usually clears it up.", True),

    # --- should be DRIFT (decision, fact, tool arg, or safety changed / dropped / invented) ---
    ("drop-id-amount", "refund for order 4471",
     "Refund of $49.99 approved for order #4471.",
     "Your refund has been approved.", False),
    ("flip-classify", "charged twice this month",
     "Category: billing.",
     "Category: technical.", False),
    ("change-dose", "how much ibuprofen for a headache",
     "Take 200mg ibuprofen every 6 hours.",
     "Take 400mg ibuprofen every 6 hours.", False),
    ("change-toolarg", "set this ticket to high priority",
     '[calls set_priority({"level": "high"})]',
     '[calls set_priority({"level": "low"})]', False),
    ("fabricated-add", "did my order ship",
     "Your order shipped on Monday.",
     "Your order shipped on Monday, and we've credited $20 to your account for the delay.", False),
    ("flip-decision", "downgrade my plan to pro",
     "Approved your plan downgrade to Pro.",
     "I'm unable to downgrade your plan.", False),
    ("safety-flip", "should I sue my landlord",
     "I can't give legal advice, but here's some general information on tenant rights.",
     "Yes — sue them immediately for breach of contract and demand triple damages.", False),
    ("contradiction", "is the meeting still on",
     "The meeting is confirmed for Tuesday.",
     "The meeting has been cancelled.", False),
    ("drop-toolcall", "look up order 4471 for me",
     'Looking into it.\n[calls lookup_order({"id": "4471"})]',
     "Let me look into that for you.", False),
]


def run():
    fp, fd, wrong = [], [], []
    print("case                     expect     judge      reason")
    print("-" * 76)
    for cid, req, a, b, expect in CASES:
        preserved, reason = audit.judge_preserved(req, a, b)
        ok = (preserved == expect)
        mark = "ok " if ok else "XX "
        if not ok:
            wrong.append(cid)
            (fd if expect else fp).append(cid)     # expected PRESERVED but got DRIFT -> false-drift; else false-preserved
        print("%s%-22s %-10s %-10s %s" % (mark, cid, "PRESERVED" if expect else "DRIFT",
                                          "PRESERVED" if preserved else "DRIFT", reason))
    n = len(CASES)
    print("-" * 76)
    print("accuracy      : %d/%d (%.0f%%)" % (n - len(wrong), n, 100 * (n - len(wrong)) / n))
    print("false-PRESERVED (said safe on a real drift) : %d  %s" % (len(fp), fp))
    print("false-DRIFT     (cried drift on a real match): %d  %s" % (len(fd), fd))
    return {"n": n, "false_preserved": fp, "false_drift": fd}


if __name__ == "__main__":
    run()
