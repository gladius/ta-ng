"""LIVE validation of the analyst AGENT vs the one-shot read, on COMPLEX controlled cases with planted truth.

The discriminating cases (all chosen so the deterministic layer is BLIND — same output SHAPE, so mixed_shapes
can't fire — and the 5-sample seed is often INSUFFICIENT, so the agent must DRIVE tools to do better):
  1 rare_2pct           98 summarize + 2 translate (both prose) — a seed-blind 2% minority mode         -> mixed
  2 two_label_classes   50 sentiment + 50 intent labels (both 'label' shape, both classifiers)           -> mixed
  3 three_prose_jobs    summarize + translate + draft-email, all prose                                    -> mixed
  4 coherent_creative   100 wildly different story openings — ONE job, max output diversity (FP trap)     -> coherent
  5 coherent_bigrag     60 RAG answers over ~4k-char retrieved-doc inputs — ONE job (FP + truncation trap)-> coherent

Uses the real PROFILE_MODEL via llm_client (live key). Reports each layer's verdict, the agent's trail (did it
drive tools?), and whether it matched truth. Precision matters as much as recall: a good analyst must NOT flag
the two coherent-but-diverse nodes as mixed.

Run: python -m spikes.validation.agent_live   (from repo root)
"""
import sys
import random
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
random.seed(11)

from app.services.profile.facts import llm_facts, _out_shape
from app.services.profile.agent import characterize_node
from app.services.profile.comprehend import comprehend_node
from app.services.profile.select import select as _select

_N = [0]


def _tr(name, user, output, system="Do the task."):
    _N[0] += 1
    tid = "%s-%04d" % (name, _N[0])
    return {"trace_id": tid, "model": "claude-sonnet-4-5", "tools_called": [],
            "input_messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "output": output}


_DOCS = ["the quarterly revenue report", "the incident post-mortem", "the vendor contract", "the research memo",
         "the customer interview notes", "the compliance audit", "the product spec", "the board deck"]
_PHRASES = ["please proceed", "thank you very much", "the meeting is at noon", "we need more time",
            "the invoice is overdue", "welcome to the team", "the flight was delayed", "call me tomorrow"]
_TOPICS = ["a lighthouse keeper", "a retired astronaut", "a city that never sleeps", "a clockmaker's apprentice",
           "the last library on earth", "a garden on Mars", "two rival chefs", "a detective cat"]


def summarize():
    d = random.choice(_DOCS)
    return ("Summarize %s for the exec brief." % d,
            "%s covers %d key points; the main takeaway is a %s outlook with follow-ups noted." % (
                d.capitalize(), random.randint(3, 6), random.choice(["cautious", "positive", "mixed", "urgent"])))


def translate():
    p = random.choice(_PHRASES)
    fr = {"please proceed": "veuillez continuer", "thank you very much": "merci beaucoup",
          "the meeting is at noon": "la reunion est a midi", "we need more time": "nous avons besoin de plus de temps",
          "the invoice is overdue": "la facture est en retard", "welcome to the team": "bienvenue dans l'equipe",
          "the flight was delayed": "le vol a ete retarde", "call me tomorrow": "appelle-moi demain"}[p]
    return ("Translate to French: %s" % p, fr)


def draft_email():
    d = random.choice(_DOCS)
    return ("Draft a follow-up email about %s." % d,
            "Hi team, following up on %s — could you review by Friday and send comments? Thanks, Alex." % d)


def sentiment():
    r = random.choice(["Loved it, works great", "Terrible, broke on day one", "It's fine, nothing special",
                       "Absolutely fantastic support", "Would not recommend", "Pretty good overall"])
    lab = "positive" if any(w in r.lower() for w in ("love", "great", "fantastic", "good")) else (
        "negative" if any(w in r.lower() for w in ("terrible", "not recommend", "broke")) else "neutral")
    return ("Classify the sentiment of this review: %s" % r, lab)


def intent():
    t = random.choice(["I want my money back", "My card was charged twice", "I can't log in",
                       "Add a seat for my colleague", "The API returns 500", "Reset my password"])
    lab = ("refund" if "money back" in t else "billing" if "charged" in t else
           "access" if ("log in" in t or "password" in t or "seat" in t) else "bug")
    return ("Route this support ticket: %s" % t, lab)


def creative():
    top = random.choice(_TOPICS)
    opener = random.choice([
        "The morning %s woke to find the tide had left something impossible on the shore.",
        "Nobody warned %s that the silence would be the hardest part.",
        "It began, as these things do, with %s and a single unanswered question.",
        "Long after the lights went out, %s kept one promise no one remembered making.",
    ])
    return ("Write the opening line of a short story about %s." % top, opener % top)


def bigrag():
    q = random.choice(["What is the refund window?", "Who approves vendor contracts?", "When did the incident start?",
                       "What are the compliance requirements?", "How is revenue recognized?"])
    docs = "\n".join("[%d] %s: %s" % (i, random.choice(_DOCS),
                                      ("Section detail. " * 40)) for i in range(1, 6))     # ~4k+ chars of context
    user = "RETRIEVED CONTEXT:\n%s\n\nUsing ONLY the context above, answer: %s" % (docs, q)
    ans = "Based on the retrieved context, %s The relevant detail is documented in the cited sections." % (
        random.choice(["the policy applies as described.", "approval sits with the named owner.",
                       "the timeline is recorded above.", "the requirements are enumerated in the audit."]))
    return _tr("coherent_bigrag", user, ans, system="Answer strictly from the retrieved context; cite sections.")


def build(name, gens):
    b = []
    for gen, cnt in gens:
        for _ in range(cnt):
            r = gen()
            if isinstance(r, dict):
                b.append(r)
            else:
                inp, out = r
                b.append(_tr(name, inp, out))
    random.shuffle(b)
    n = {"key": name, "node": name, "graph_path": "", "model": "claude-sonnet-4-5", "calls": len(b)}
    n["out_type"] = Counter(_out_shape(str(t["output"])) for t in b).most_common(1)[0][0]
    return n, b


CASES = [
    ("rare_2pct", [(summarize, 98), (translate, 2)], "mixed"),
    ("two_label_classes", [(sentiment, 50), (intent, 50)], "mixed"),
    ("three_prose_jobs", [(summarize, 34), (translate, 33), (draft_email, 33)], "mixed"),
    ("coherent_creative", [(creative, 100)], "coherent"),
    ("coherent_bigrag", [(bigrag, 60)], "coherent"),
]


def _v(c):
    return (c.get("coherence") or {}).get("verdict") or "?"


def _match(verdict, truth):
    got_mixed = verdict == "possible_mixed_bucket"
    return (got_mixed and truth == "mixed") or (not got_mixed and truth == "coherent")


def main():
    print("LIVE agent vs one-shot — COMPLEX planted cases · deterministic-blind same-shape · recall + precision\n")
    tally = {"one-shot": 0, "agent": 0}
    for name, gens, truth in CASES:
        n, b = build(name, gens)
        f = llm_facts(n, b, [])
        print("== %s  (truth=%s) ==" % (name, truth))
        print("   facts: shapes=%s mixed_shapes=%s distinct_out=%d/%d avg_in~%dch"
              % (f["output_shapes"], f["mixed_shapes"], f["distinct_output"], f["n"],
                 sum(len(m.get("content", "")) for t in b[:1] for m in t["input_messages"])))
        try:
            c1 = comprehend_node(n, _select(b, n=5))
            v1 = _v(c1)
            ok1 = _match(v1, truth)
            tally["one-shot"] += ok1
            print("   one-shot : verdict=%-22s op=%-12s %s" % (v1, c1.get("op"), "MATCH" if ok1 else "MISS"))
        except Exception as e:
            print("   one-shot : FAILED %s" % str(e)[:110])
        try:
            c2 = characterize_node(n, b, f, edges=[])
            v2 = _v(c2)
            ok2 = _match(v2, truth)
            tally["agent"] += ok2
            print("   agent    : verdict=%-22s op=%-12s %s  | inspections=%d trail=%s"
                  % (v2, c2.get("op"), "MATCH" if ok2 else "MISS", c2["inspections"], c2["trail"]))
        except Exception as e:
            print("   agent    : FAILED %s" % str(e)[:130])
        print()
    print("SCORE  one-shot=%d/%d   agent=%d/%d" % (tally["one-shot"], len(CASES), tally["agent"], len(CASES)))


if __name__ == "__main__":
    main()
