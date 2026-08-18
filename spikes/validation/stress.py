"""STRESS validation — realistic failure cases at scale, not one-liners. ~100 calls/node, RARE minority modes
(5%), interleaved mixes, and LARGE multi-paragraph content. Mixed-detection is now a DETERMINISTIC fact over ALL
N ($0), so it should catch rare modes a 5-sample read never would. One LLM call tests large-content truncation.

Run: python -m spikes.validation.stress   (from repo root)
"""
import sys
import random

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
random.seed(23)

from connectors.graph import build_graph
from app.services.profile.facts import fact_pack
from app.services.profile.select import select as _select
from app.services.profile.comprehend import comprehend_node

_N = [0]
LABELS = ["billing_dispute", "refund_request", "security_incident", "access_request", "integration_bug"]
_BODIES = ["a double charge dispute", "a locked account", "a teammate seat request", "a phishing report",
           "a slow dashboard", "a failed webhook", "a data residency question", "a refund delay"]


def _tr(node, system, user, output):
    _N[0] += 1
    tid = "s%04d" % _N[0]
    return {"id": tid, "trace_id": tid, "parent_run_id": None, "dotted_order": "%05d" % _N[0],
            "run_type": "llm", "name": node, "start_time": "", "end_time": "",
            "agent_hint": "stress", "node_hint": node,
            "trace": {"trace_id": tid, "agent_id": "stress", "node_id": node, "graph_path": "", "error": "",
                      "input_messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                      "output": output, "tools_called": [],
                      "usage": {"input_tokens": max(1, len(user) // 4), "output_tokens": max(1, len(output) // 4)},
                      "model": "claude-sonnet-4-5"}}


def _label():
    return random.choice(LABELS)


def _reply():
    return "Hi — sorry about %s. Here's what we'll do: first we verify the account, then we apply the fix, and you should be set within 3-5 business days. Let me know if anything's unclear." % random.choice(_BODIES)


def _bigdoc():                                                     # ~3k-char multi-paragraph input (NOT a one-liner)
    para = ("The customer submitted a detailed report describing %s across several sessions, including timestamps, "
            "affected order ids, and prior support interactions that did not resolve the underlying problem. ")
    return "SUMMARIZE THIS CASE FILE:\n" + "".join(para % random.choice(_BODIES) for _ in range(12))


def corpus():
    R, truth = [], {}
    sysc = "Classify the ticket into one category."
    sysh = "Handle the incoming support request."

    # 1) mixed_ordered — 50 labels THEN 50 prose (the ordering that broke the sampler)
    for _ in range(50):
        R.append(_tr("mixed_ordered", sysh, "Classify: %s" % random.choice(_BODIES), _label()))
    for _ in range(50):
        R.append(_tr("mixed_ordered", sysh, "Reply to the customer about their issue", _reply()))
    truth["mixed_ordered"] = True

    # 2) mixed_rare — 95 labels + 5 prose (a 5% minority mode; a 5-sample read misses it ~almost always)
    calls = [("Classify: %s" % random.choice(_BODIES), _label()) for _ in range(95)] + \
            [("Reply to the customer", _reply()) for _ in range(5)]
    random.shuffle(calls)
    for u, o in calls:
        R.append(_tr("mixed_rare", sysh, u, o))
    truth["mixed_rare"] = True

    # 3) mixed_interleaved — alternating
    for i in range(100):
        if i % 2:
            R.append(_tr("mixed_interleaved", sysh, "Reply about the issue", _reply()))
        else:
            R.append(_tr("mixed_interleaved", sysh, "Classify: %s" % random.choice(_BODIES), _label()))
    truth["mixed_interleaved"] = True

    # 4) coherent_classifier — 100 labels, ONE mode (control: must NOT flag mixed)
    for _ in range(100):
        R.append(_tr("coherent_classifier", sysc, "Ticket: %s" % random.choice(_BODIES), _label()))
    truth["coherent_classifier"] = False

    # 5) coherent_bigprose — LARGE multi-paragraph inputs + prose outputs (coherent; tests truncation)
    for _ in range(60):
        R.append(_tr("coherent_bigprose", "Summarize the case file for the agent.", _bigdoc(),
                     "The case concerns %s with repeated unresolved contacts; recommend escalation and a refund review." % random.choice(_BODIES)))
    truth["coherent_bigprose"] = False

    # 6) semantic_mixed — SAME shape (all prose) but two DIFFERENT jobs (honest residual the shape-fact can't see)
    for _ in range(50):
        R.append(_tr("semantic_mixed", "Do the task.", "Summarize this legal contract clause ...",
                     "The clause sets termination terms, liability caps, and a 30-day cure period."))
    for _ in range(50):
        R.append(_tr("semantic_mixed", "Do the task.", "Draft a marketing tagline for a coffee brand ...",
                     "Wake up to bold mornings — brewed fresh, crafted for you."))
    truth["semantic_mixed"] = "same-shape (residual)"

    return R, truth


def main():
    records, truth = corpus()
    G = build_graph(records, agent_default="stress")
    g = {"nodes": G.nodes, "structural_nodes": G.structural_nodes, "buckets": dict(G.buckets), "edges": G.edges}
    fp = fact_pack(g)

    print("STRESS validation — ~100 calls/node · rare modes · large content · deterministic mixed-fact ($0)\n")
    for key, f in fp.items():
        name = f["node"]
        want = truth.get(name)
        if want is None or f["kind"] != "llm":
            continue
        got = f["mixed_shapes"]
        if want is True:
            ok = "PASS" if got else "FAIL"
        elif want is False:
            ok = "PASS" if not got else "FAIL (false positive)"
        else:
            ok = "residual — shape-fact can't see (needs the read)" + (" [and it didn't false-flag]" if not got else " [FALSE FLAG]")
        print("  %-20s n=%-3d shapes=%-24s mixed_shapes=%-5s -> %s"
              % (name, f["n"], str(f["output_shapes"]), got, ok))

    # large-content: confirm comprehend + truncation actually work on multi-paragraph input (1 LLM call)
    bp = next(k for k, f in fp.items() if f["node"] == "coherent_bigprose")
    print("\n--- large-content read (truncation test, 1 LLM call) ---")
    try:
        c = comprehend_node({"node": "coherent_bigprose", "key": bp, "graph_path": "", "model": "claude-sonnet-4-5",
                             "calls": fp[bp]["n"], "out_type": fp[bp]["output_type"]}, _select(g["buckets"][bp], n=5))
        print("  op:", c.get("op"), "| coherence:", (c.get("coherence") or {}).get("verdict"))
        print("  summary:", (c.get("summary") or "")[:150])
    except Exception as e:
        print("  FAILED on large content:", str(e)[:120])


if __name__ == "__main__":
    main()
