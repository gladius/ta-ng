"""Controlled validation of the analyst piece — planted ground-truth corpus, run through the REAL path
(build_graph -> fact_pack -> comprehend -> guards), scored against truth. Offline; needs an LLM key for the
comprehend calls (~7). Includes the ADVERSARIAL cases (templated / dynamic-system / mixed bucket) — easy nodes
prove nothing.

Run: python -m spikes.validation.run   (from repo root)
"""
import sys
import random

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
random.seed(11)

from connectors.graph import build_graph
from app.services.profile.facts import fact_pack
from app.services.profile.comprehend import comprehend_node
from app.services.profile.select import select as _select
from app.services.profile.guards import reconcile

_ORD = [0]


def _tr(node, system, user, output, tools=None):
    _ORD[0] += 1
    tid = "t%03d" % _ORD[0]
    return {"id": tid, "trace_id": tid, "parent_run_id": None, "dotted_order": "%03d" % _ORD[0],
            "run_type": "llm", "name": node, "start_time": "", "end_time": "",
            "agent_hint": "val", "node_hint": node,
            "trace": {"trace_id": tid, "agent_id": "val", "node_id": node, "graph_path": "", "error": "",
                      "input_messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                      "output": output, "tools_called": tools or [],
                      "usage": {"input_tokens": 500, "output_tokens": len(output) // 4}, "model": "claude-sonnet-4-5"}}


def corpus():
    """(records, truth). truth[node] = {ops(acceptable), mixed, templated, frame_varies}."""
    R, truth = [], {}
    orders = ["order-%d" % random.randint(100, 999) for _ in range(40)]

    # 1) ROUTER (clear) — fixed system, short label out of a small set
    sysr = "You are a support router. Read the ticket and output exactly ONE category."
    cats = ["billing_dispute", "refund_request", "security_incident", "access_request"]
    for i in range(12):
        R.append(_tr("router", sysr, "Ticket: my %s is wrong on %s" % (random.choice(["charge", "login", "refund", "seat"]), random.choice(orders)), random.choice(cats)))
    truth["router"] = {"ops": {"router", "classifier", "dispatcher", "routing", "triage"}, "mixed": False, "templated": False, "frame_varies": False}

    # 2) TOOL-CALLER (clear) — fixed system, emits a tool call
    syst = "You are the account worker. Call lookup_account when you need account data."
    for i in range(11):
        oid = random.choice(orders)
        R.append(_tr("account_worker", syst, "Resolve the dispute on %s" % oid,
                     "Looking it up.", tools=["lookup_account"]))
    truth["account_worker"] = {"ops": {"tool_caller", "tool_user", "tool", "agent", "executor", "worker"}, "mixed": False, "templated": False, "frame_varies": False}

    # 3) SUMMARIZER (clear free-form) — fixed system, varied prose
    syss = "Summarize the customer's issue in 1-2 sentences for the agent."
    bodies = ["a double charge dispute that needs a refund", "a locked account after too many logins",
              "a request to add a teammate seat", "a suspected phishing email", "a slow dashboard after an update"]
    for i in range(10):
        b = random.choice(bodies)
        R.append(_tr("summarizer", syss, "Customer wrote a long message about %s ..." % b,
                     "The customer reports %s and asks for help resolving it." % b))
    truth["summarizer"] = {"ops": {"summarizer", "summariser", "generator", "writer", "condenser"}, "mixed": False, "templated": False, "frame_varies": False}

    # 4) TEMPLATED (adversarial) — fixed system, output is a template with a per-call id/date -> should read TEMPLATED
    syst2 = "Confirm the refund to the customer."
    for i in range(10):
        oid, day = random.choice(orders), random.randint(1, 28)
        R.append(_tr("confirmer", syst2, "Confirm refund for %s" % oid,
                     "Your refund for %s has been processed on 2026-08-%02d and will arrive in 3-5 days." % (oid, day)))
    truth["confirmer"] = {"ops": {"responder", "notifier", "generator", "confirmer", "messenger"}, "mixed": False, "templated": True, "frame_varies": False}

    # 5) DYNAMIC-SYSTEM (adversarial) — the SYSTEM varies per call (injected account/ticket) -> distinct_system>1
    for i in range(10):
        acct, tid = random.randint(1000, 9999), random.randint(1, 99)
        sysd = "Account: %d. Ticket: T-%d. You are the support agent; resolve using the account context above." % (acct, tid)
        R.append(_tr("support_agent", sysd, "Help me with my issue please.",
                     "I've reviewed your account and resolved the issue; you should be all set now."))
    truth["support_agent"] = {"ops": {"support", "handler", "responder", "assistant", "agent"}, "mixed": False, "templated": False, "frame_varies": True}

    # 6) MIXED BUCKET (adversarial) — ONE node, secretly TWO ops: half classify, half free-form answer
    sysm = "Handle the incoming support request."
    for i in range(6):
        R.append(_tr("assist", sysm, "Classify this ticket: %s" % random.choice(bodies), random.choice(cats)))
    for i in range(6):
        b = random.choice(bodies)
        R.append(_tr("assist", sysm, "Write a reply to the customer about %s" % b,
                     "Hi! I'm sorry about %s — here's how we'll fix it, step by step ..." % b))
    truth["assist"] = {"ops": set(), "mixed": True, "templated": False, "frame_varies": False}   # test the MIXED flag

    # 7) AMBIGUOUS (adversarial) — short label out; router or classifier both acceptable
    sysa = "Label the sentiment of the message as positive, negative, or neutral."
    for i in range(10):
        R.append(_tr("labeler", sysa, "Message: %s" % random.choice(["thanks so much!", "this is broken", "ok", "terrible service", "works fine"]),
                     random.choice(["positive", "negative", "neutral"])))
    truth["labeler"] = {"ops": {"classifier", "router", "labeler", "sentiment", "labeller"}, "mixed": False, "templated": False, "frame_varies": False}

    return R, truth


def _op_ok(op, ops):
    o = (op or "").lower()
    return bool(o) and (o in ops or any(x in o or o in x for x in ops))


def main():
    records, truth = corpus()
    G = build_graph(records, agent_default="val")
    g = {"nodes": G.nodes, "structural_nodes": G.structural_nodes, "buckets": dict(G.buckets),
         "edges": G.edges, "traces": G.trace_count}
    fp = fact_pack(g)

    print("Controlled validation — %d nodes, planted truth, real path (facts -> comprehend -> guards)\n" % len(truth))
    passed = 0
    total = 0
    for key, f in fp.items():
        name = f["node"]
        t = truth.get(name)
        if t is None or f["kind"] != "llm":
            continue
        total += 1
        bucket = g["buckets"][key]
        node = {"node": name, "key": key, "graph_path": "", "model": "claude-sonnet-4-5",
                "calls": f["n"], "out_type": f["output_type"]}
        try:
            comp = comprehend_node(node, _select(bucket, n=5))
        except Exception as e:
            print("  %-16s COMPREHEND FAILED: %s" % (name, str(e)[:80])); continue
        rec = reconcile(comp, f, bucket)
        mixed_flagged = rec["coherence"] == "possible_mixed_bucket" or any("MIXED" in x for x in rec["flags"])
        frame_flagged = any("system varies" in x for x in rec["flags"])

        # score by case type
        if t["mixed"]:
            ok = mixed_flagged; want = "flag MIXED"
        elif t["templated"]:
            ok = f["distinct_output"] <= 3 and _op_ok(comp["op"], t["ops"]); want = "TEMPLATED (low distinct-out)"
        elif t["frame_varies"]:
            ok = f["distinct_system"] > 1 and frame_flagged and _op_ok(comp["op"], t["ops"]); want = "DYNAMIC frame + right op"
        else:
            ok = _op_ok(comp["op"], t["ops"]); want = "op ∈ %s" % sorted(t["ops"])[:3]
        passed += ok
        print("  %-15s op=%-12s d_sys=%d d_out=%-2d cite=%s | want: %-28s %s"
              % (name, comp.get("op"), f["distinct_system"], f["distinct_output"], rec["citation_ok"], want,
                 "PASS" if ok else "FAIL"))
        if rec["flags"]:
            print("       guards: " + " · ".join(rec["flags"]))

    print("\nSCORE: %d/%d passed" % (passed, total))


if __name__ == "__main__":
    main()
