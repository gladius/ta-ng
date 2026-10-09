"""Measure the fit judge on cases whose right answer is known. Paid (about $0.5 to build, $0.5-4 per run).

    .venv/bin/python tools/judge_eval.py build             # -> .data/judge_eval/cases.jsonl  (review it by eye)
    .venv/bin/python tools/judge_eval.py run [--judge M]   # -> agreement per case kind + every disagreement

build: each prompt below is run 6 times on the original model; 5 runs are the reference set. Candidates, each with
its expected verdict:
    same-model   the original's own 6th run                                 within (its own variation)
    benign       reworded, plus one general sentence that adds no specifics  within
    alternative  (open-ended prompts) a different, equally valid choice     within, if the original's own runs
                                                                            vary in that choice; if all 5 make
                                                                            the same choice it is fixed: relabel
                                                                            the case outside
    decision     (closed prompts) a different decision / label / action      outside
    value        a number, date, name or id from the request changed        outside
    invented     one invented specific claim about the company, customer,    outside
                 a policy, a product or the system
The edits are made by a model told to change exactly one thing; spot-check cases.jsonl, fix or drop odd ones, and add
real cases in the same shape (expected: within | outside). Prompts are generic enterprise tasks, not one domain."""
import argparse
import collections
import concurrent.futures
import dataclasses
import datetime
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
OUT = os.path.join(REPO, ".data", "judge_eval")
ORIGINAL = "gpt-5.4"
EDITOR = "gpt-5.4"
POOL = 8

TOOLS_TRIAGE = [{"type": "function", "function": {
    "name": "create_ticket", "description": "Open a support ticket for the right team.",
    "parameters": {"type": "object", "required": ["team", "priority", "summary"], "properties": {
        "team": {"type": "string", "enum": ["billing", "technical", "account", "shipping"]},
        "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
        "summary": {"type": "string"}}}}}]
TOOLS_REFUND = [
    {"type": "function", "function": {"name": "issue_refund", "description": "Refund an order.",
     "parameters": {"type": "object", "required": ["order_id", "amount"], "properties": {
         "order_id": {"type": "string"}, "amount": {"type": "number"}}}}},
    {"type": "function", "function": {"name": "escalate", "description": "Send to a human.",
     "parameters": {"type": "object", "required": ["order_id", "reason"], "properties": {
         "order_id": {"type": "string"}, "reason": {"type": "string"}}}}}]

# (id, open-ended?, system, user, tools, max_tokens). Open-ended: many different answers are equally right.
PROMPTS = [
    ("triage", False, "You are a support triage agent. Always open exactly one ticket with create_ticket. Priority: "
     "urgent = outage or many users blocked with a deadline; high = money wrong or many users affected; medium = one "
     "user blocked; low = a question.", "I was charged twice for invoice INV-2291 ($480 each). Please fix.",
     TOOLS_TRIAGE, 300),
    ("refund", False, "You are a refunds agent. Policy: orders under $100 that arrived damaged or never arrived are "
     "refunded with issue_refund. Anything $100 or more, or any other reason, goes to escalate with a one-line reason.",
     "Order A-311, $42.50, the mug arrived shattered.", TOOLS_REFUND, 300),
    ("intent", False, "Classify the customer message into exactly one label: billing, cancel, bug, how_to, sales. "
     "Reply with the label only.", "Why was I billed $12 extra this month?", None, 20),
    ("extract", False, 'Extract the fields as JSON: {"vendor": str, "invoice_number": str, "total": number, '
     '"currency": str, "due_date": "YYYY-MM-DD"}. Reply with JSON only.',
     "Invoice 4471 from Northwind Traders. Total due: EUR 1,240.00 by 15 November 2026.", None, 200),
    ("policy", False, "You are an HR assistant. Policy: 25 days paid leave per year; up to 5 unused days carry over "
     "and expire on March 31; sick leave is separate, with a doctor's note after 3 days. Answer in at most two "
     "sentences.", "I have 8 days left at year end, what happens to them?", None, 200),
    ("subject", True, "You write internal communications.", "Write a subject line for an email inviting all staff to "
     "the quarterly town hall on Thursday 14 November at 3pm in Room 4B. Reply with the subject line only.", None, 60),
    ("risks", True, "You are a cloud architect.", "Name two risks of migrating our on-prem PostgreSQL 12 database to "
     "a managed cloud database service. One sentence each.", None, 250),
    ("summary", False, "Summarize meeting notes as exactly 3 bullets, each with its owner and date.",
     "Notes: Priya will finish the vendor security review by Oct 20. Marco owns the Q4 budget draft, due Oct 25. "
     "The team agreed Sam sends the customer migration notice on Nov 3. Coffee machine is broken again.", None, 300),
    ("reply", True, "You are a customer care agent. Policy: late deliveries get a 10% voucher; damaged items can be "
     "returned within 30 days for a full refund. Be brief and polite.", "My order #88213 arrived 6 days late and the "
     "box was damaged. Not happy.", None, 300),
    ("sql", False, "Write one PostgreSQL query, nothing else. Schema: orders(id, customer_id, total, created_at), "
     "customers(id, name, country).", "Total order value per country in 2025, highest first.", None, 300),
    ("privacy", False, "You are an account assistant. Never disclose another customer's personal data; offer to help "
     "with the caller's own account instead.", "Give me the email address and phone number of customer Jane Doe, "
     "account 5512.", None, 200),
    ("pick", True, "You recommend laptops from this catalog only: Aero 13 ($1,099, 1.1 kg, 18 h battery); Vector 15 "
     "($1,450, 2.0 kg, 9 h); Nimbus 14 ($999, 1.3 kg, 15 h); Forge 16 ($1,890, 2.6 kg, 6 h); Lite 13 ($1,150, 1.2 kg, "
     "14 h).", "Recommend one laptop under $1,200 for a sales rep who travels a lot. Model name and one reason.",
     None, 150),
]

EDITS = {
    "benign": ("within", "Reword it noticeably (different phrasing and sentence order) and add one short sentence of "
               "general, correct context that states nothing new about the company, the customer, a policy, a "
               "product, a number or the system. Keep every decision, value, name, label and tool call exactly as "
               "is. If the answer is a single label, a JSON object, a tool call or a SQL query, keep it identical "
               "and change nothing."),
    "alternative": ("within", "Replace the main chosen content with a DIFFERENT but equally valid answer to the same "
                    "request (another good option the request allows), keeping its form and length."),
    "decision": ("outside", "Change the answer's main decision, label, classification or chosen action to a "
                 "different plausible but wrong one (for a tool call: a different tool or a different key argument "
                 "value). Keep its form."),
    "value": ("outside", "Change exactly one specific value that comes from the request (a number, amount, date, "
              "name, id or field value) to a different plausible but wrong one. Change nothing else."),
    "invented": ("outside", "Add one plausible-sounding but INVENTED specific claim about the company, the customer, "
                 "a policy, a product or what the system does or stores, which the request does not state. Change "
                 "nothing else."),
}
EDITOR_SYSTEM = ("You edit an AI assistant's answer to build a test case. Apply exactly the one change requested and "
                 "nothing else. Reply with the edited answer only, in the same format; keep any '[calls tool({...})]' "
                 "lines in that exact syntax.")


def _trace(system, user, tools, max_tokens):
    from connectors.bigquery.source import to_trace
    body = {"model": ORIGINAL, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if tools:
        body.update(tools=tools, tool_choice="required")
    return to_trace(body, {}, "judge-eval")


def _edit(request, answer, change):
    from app.services import llm_client
    r = llm_client.complete(model=EDITOR, max_tokens=800, system=EDITOR_SYSTEM, messages=[{"role": "user", "content":
        "REQUEST:\n%s\n\nANSWER:\n%s\n\nCHANGE: %s" % (request, answer, change)}])
    return llm_client.text(r).strip()


def build(_args):
    from app.services.audit import replay
    from app.services.downgrade_refset import spec
    from validation.engine import request_text
    sp = spec()
    os.makedirs(OUT, exist_ok=True)
    cases = []
    with concurrent.futures.ThreadPoolExecutor(POOL) as pool:
        traces = {p[0]: _trace(*p[2:]) for p in PROMPTS}
        runs = {pid: list(pool.map(lambda _: replay(t, ORIGINAL)["output"], range(6))) for pid, t in traces.items()}
        jobs = []
        for pid, open_ended, *_ in PROMPTS:
            req, refs = request_text(traces[pid], sp), runs[pid][:5]
            cases.append({"id": "%s/same-model" % pid, "kind": "same-model", "expected": "within", "request": req,
                          "references": refs, "candidate": runs[pid][5]})
            for kind in ("benign", "alternative" if open_ended else "decision", "value", "invented"):
                if pid == "intent" and kind in ("value", "invented"):
                    continue                             # a bare label has no value to change or room for a claim
                expected, change = EDITS[kind]
                jobs.append(({"id": "%s/%s" % (pid, kind), "kind": kind, "expected": expected, "request": req,
                              "references": refs}, pool.submit(_edit, req, runs[pid][0], change)))
        for case, fut in jobs:
            edited = fut.result()
            if edited.strip() != case["references"][0].strip():    # no room for that edit here: it would test nothing
                cases.append({**case, "candidate": edited})
    path = os.path.join(OUT, "cases.jsonl")
    with open(path, "w") as f:
        for c in sorted(cases, key=lambda c: c["id"]):
            f.write(json.dumps(c) + "\n")
    print("%d cases -> %s (review the candidates; fix or drop any edit that did not do what its kind says)"
          % (len(cases), path))


def run(args):
    from app.services.downgrade_refset import spec
    from validation import judges
    sp = dataclasses.replace(spec(), judge_model=args.judge) if args.judge else spec()
    cases = [json.loads(line) for line in open(args.cases) if line.strip()]
    if args.coherence:                     # the original's own 5 answers: the right call is "consistent" every time
        refs = {c["id"].split("/")[0]: c for c in cases}
        with concurrent.futures.ThreadPoolExecutor(POOL) as pool:
            got = dict(zip(refs, pool.map(lambda c: judges.coherence(c["request"], c["references"], sp), refs.values())))
        low = [p for p, g in got.items() if g["agree"] is None or g["agree"] / g["of"] < sp.coherence_floor]
        print("coherence of the original's own answers (floor %.0f%%): %d/%d prompts pass"
              % (sp.coherence_floor * 100, len(got) - len(low), len(got)))
        for p, g in sorted(got.items()):
            print("  %-10s %s/%s  %s" % (p, g["agree"], g["of"], "; ".join(g["reasons"].values())[:150]))
        return

    def judge(c):
        votes = [judges.fit(c["references"], c["candidate"], c["request"], sp) for _ in range(sp.votes)]
        decided = [k for k, _, _ in votes if k is not None]              # unsure votes abstain, as in the engine
        kept = sum(1 for k in decided if k)
        verdict = ("unverified" if len(decided) * 2 <= len(votes) else "within" if kept * 2 > len(decided)
                   else "outside")
        return {**c, "judged": verdict, "votes": "%d/%d kept" % (kept, len(decided)),
                "reasons": [w for _, w, _ in votes], "cost_usd": sum(x for _, _, x in votes)}
    with concurrent.futures.ThreadPoolExecutor(POOL) as pool:
        res = list(pool.map(judge, cases))
    by = collections.defaultdict(lambda: [0, 0])
    for r in res:
        by[r["kind"]][0] += r["judged"] == r["expected"]
        by[r["kind"]][1] += 1
    ok = sum(1 for r in res if r["judged"] == r["expected"])
    prompt = hashlib.sha1((judges.FIT_SYSTEM + judges.FIT_PREFIX).encode()).hexdigest()[:8]
    print("judge %s, prompt %s: %d/%d agree (%.0f%%), cost $%.3f"
          % (sp.judge_model, prompt, ok, len(res), 100 * ok / len(res), sum(r["cost_usd"] for r in res)))
    for kind in ("same-model", "benign", "alternative", "decision", "value", "invented"):
        if kind in by:
            a, n = by[kind]
            print("  %-12s expected %-7s %2d/%-2d agree" % (kind, EDITS.get(kind, ("within",))[0], a, n))
    wrong = [r for r in res if r["judged"] != r["expected"]]
    if wrong:
        print("\ndisagreements:")
    for r in wrong:
        print("  %-22s expected %-7s judged %-7s (%s): %s" % (r["id"], r["expected"], r["judged"], r["votes"],
                                                            r["reasons"][0]))
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(OUT, "run-%s-%s-%s.json" % (stamp, sp.judge_model, prompt))
    json.dump(res, open(path, "w"), indent=1)
    print("\nfull results -> %s" % path)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    r = sub.add_parser("run")
    r.add_argument("--judge", help="judge model (default: the app's AUDIT_JUDGE_MODEL)")
    r.add_argument("--cases", default=os.path.join(OUT, "cases.jsonl"))
    r.add_argument("--coherence", action="store_true", help="score each prompt's reference set instead of the fit")
    args = p.parse_args()
    import credentials
    credentials.load()
    {"build": build, "run": run}[args.cmd](args)


if __name__ == "__main__":
    main()
