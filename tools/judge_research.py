"""Where is the fit judge wrong? Cases whose right answer is known by construction, judged by prompt variants.
Every model call is cached in .data/judge_research/calls.jsonl, so a rerun costs nothing.

    .venv/bin/python tools/judge_research.py cases                    # about $0.2 -> .data/judge_research/cases.jsonl
    .venv/bin/python tools/judge_research.py run J0 J0v --max-usd 3

Two families of cases, each with edits whose label is certain:
  eval        judge_eval's set (12 prompts, 5 sampled references each)
  structured  JSON / tool-call answers with a free-text field, some after a tool result (STRUCTURED below); the
              original (gpt-5.4) answers 6 times: 5 references, the 6th is the same-model case
    must pass   json (keys reordered, re-spaced), contractions, bold (emphasis removed), tense (one or two words: a
                tense or a synonym; checked by a word diff and by eye), field-contractions / field-tense (the same,
                inside a JSON free-text field), plus judge_eval's same-model / benign / alternative
    must fail   number (a number taken from the request changed), dropkey (a JSON field removed), enum (a field set
                to another option the request lists), dropfact (a date or amount every reference states, removed
                from a free-text field), plus judge_eval's decision / value / invented
Each edit is also judged against a FROZEN reference set: the first reference 5 times, as an original answering word
for word the same every time does (temperature 0, a tight prompt).

Variants: J0 the judge's prompt and its view of the request; J0v the same prompt with the full view (tool
definitions, tool calls and results, every turn). Rerun both before changing the judge's prompt or model: answers
already judged come from the cache, so only the changed calls are paid.
Measured on 2026-10-10 (Sonnet 5, 233 cases): the earlier prompt passed 7 must-fail cases; with the four rules
now in judges.FIT_SYSTEM and the full view, 1 (a ticket summary leaving out an amount, judged acceptable), and no
new false drift. A "wording is never fixed" prompt did worse and was dropped.
"""
import argparse
import collections
import concurrent.futures
import dataclasses
import difflib
import hashlib
import json
import os
import re
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
OUT = os.path.join(REPO, ".data", "judge_research")
EVAL_CASES = os.path.join(REPO, ".data", "judge_eval", "cases.jsonl")
EDITOR = "gpt-5.4"
POOL = 8
RELABEL = {"pick/alternative": "outside"}    # every reference picks Aero 13: a fixed choice (judge_eval's own rule)
# tense edits read by eye: the word diff checks size, not meaning
REVIEWED = {"pick/tense": None,                       # "battery life" -> "battery capacity": not the same meaning
            "reply/tense": None,                      # "like to return it" -> "like to initiate it": unclear
            "sql/tense": ("boundary", "outside"),     # "<" -> "<=": now counts orders on 1 January 2026
            "hrjson/field-tense": None}               # "expire on March 31" -> "expire by March 31": not the same
ORIGINAL = "gpt-5.4"


def _lookup(call_id, order, result):
    return [{"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {
        "name": "lookup_order", "arguments": json.dumps({"order": order})}}]},
            {"role": "tool", "tool_call_id": call_id, "content": result}]


def _tool(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": {p: {"type": t} for p, t in props.items()}, "required": required}}}


TOOLS_LOOKUP = [_tool("lookup_order", "Look up an order", {"order": "integer"}, ["order"])]
TOOLS_EMAIL = [_tool("send_email", "Send an email", {"to": "string", "subject": "string", "body": "string"},
                     ["to", "subject", "body"])]
TOOLS_EVENT = [_tool("update_event", "Change a calendar event", {"event_id": "string", "start": "string",
                                                                 "note": "string"}, ["event_id", "start"])]
# (id, system, user, turns after the user's, tools, JSON reply?)  order12 is tools-json from val_e0cd454a8fde.
STRUCTURED = [
    ("order12", 'Support agent. Reply in JSON {"status": str, "reply": str}.', "Where is order 12? Answer in json.",
     _lookup("c1", 12, "order 12: shipped 2026-10-07, arrives 2026-10-10"), TOOLS_LOOKUP, True),
    ("order57", 'Support agent. Reply in JSON {"status": str, "reply": str}.', "Where is order 57? Answer in json.",
     _lookup("c1", 57, "order 57: delayed at Leipzig hub, new ETA 2026-10-16, carrier DHL"), TOOLS_LOOKUP, True),
    ("delayed", 'You update customers on orders. Reply in json {"order_id": str, "state": "in_transit"|"delivered"|'
     '"delayed", "customer_message": str}.', "Customer asks about order 4471.",
     _lookup("c1", 4471, "order 4471: delayed at hub, new ETA 2026-10-14"), TOOLS_LOOKUP, True),
    ("ticket", 'Classify the support message. Reply in json {"category": "billing"|"technical"|"account", '
     '"priority": "low"|"medium"|"high", "reason": str}.', "Our invoices page shows a 500 error since this morning "
     "and I can't download last month's invoice for our auditors.", [], None, True),
    ("claim", 'Expense policy: claims up to $200 with a receipt are approved; without a receipt, refer to a manager; '
     'over $200, refer. Reply in json {"decision": "approve"|"deny"|"refer", "explanation": str}.',
     "Claim: taxi to the client site, $64.20, receipt attached.", [], None, True),
    ("incident", 'Write an incident status update. sev1 = many customers cannot pay or log in. Reply in json '
     '{"severity": "sev1"|"sev2"|"sev3", "summary": str, "next_update_minutes": int}.',
     "Checkout API error rate 38% for 20 minutes; all EU customers affected.", [], None, True),
    ("hrjson", 'HR assistant. Policy HR-4.2: 25 days paid leave per year. Policy HR-4.3: up to 5 unused days carry '
     'over and expire on March 31. Reply in json {"answer": str, "policy_ref": str}.',
     "Can I carry over 7 days into next year?", [], None, True),
    ("email", "You are an executive assistant. Use send_email for every request.", "Tell jordan@acme.com the product "
     "demo moved to Friday 17 October at 3pm, same video link.", [], TOOLS_EMAIL, False),
    ("event", "You manage the calendar with update_event.", "Move event EVT-208 (vendor review) to 2026-10-21T14:00 "
     "and note that the security team joins.", [], TOOLS_EVENT, False),
]
# the real candidates of tools-json: mini changed one tense (fine), nano changed the status and dropped a date
REAL = {"order12": [
    ("real-mini", "within", '{"status":"ok","reply":"Order 12 has shipped on 2026-10-07 and is expected to arrive on '
                            '2026-10-10."}'),
    ("real-nano", "outside", '{"status":"success","reply":"Order 12 has shipped and is expected to arrive on '
                             '2026-10-10."}')]}

# --- edits made by code ------------------------------------------------------------------------------------------
TOOL_LINE = re.compile(r"^\[calls (\w+)\((.*)\)\]$", re.S)
CONTRACTIONS = [("can't", "cannot"), ("don't", "do not"), ("won't", "will not"), ("I'm", "I am"),
                ("you're", "you are"), ("we're", "we are"), ("they're", "they are"), ("isn't", "is not"),
                ("aren't", "are not"), ("doesn't", "does not"), ("didn't", "did not"), ("wasn't", "was not"),
                ("weren't", "were not"), ("haven't", "have not"), ("hasn't", "has not"), ("I'll", "I will"),
                ("we'll", "we will"), ("you'll", "you will"), ("I've", "I have"), ("we've", "we have"),
                ("you've", "you have")]                                 # unambiguous ones only (not it's, I'd)


def as_json(s):
    """-> (tool name or '', parsed object) when the answer is one JSON object or one tool-call line, else None."""
    s = s.strip()
    m = TOOL_LINE.match(s)
    try:
        obj = json.loads(m.group(2) if m else s)
    except ValueError:
        return None
    return (m.group(1) if m else "", obj) if isinstance(obj, dict) else None


def _dump(name, obj, **kw):
    body = json.dumps(obj, ensure_ascii=False, **kw)
    return "[calls %s(%s)]" % (name, body) if name else body


def _reorder(o):
    if isinstance(o, dict):
        return {k: _reorder(o[k]) for k in reversed(o)}
    return [_reorder(x) for x in o] if isinstance(o, list) else o


def edit_json(base, _req):
    j = as_json(base)
    return _dump(j[0], _reorder(j[1]), indent=2) if j and len(j[1]) > 1 else None


def edit_dropkey(base, _req):
    j = as_json(base)
    return _dump(j[0], {k: v for k, v in j[1].items() if k != list(j[1])[-1]}) if j and len(j[1]) > 1 else None


def _swap_case(src, dst):
    return lambda m: dst[0].upper() + dst[1:] if m.group(0)[0].isupper() else dst


def edit_contractions(base, _req):
    if as_json(base):
        return None
    out = base
    contracted = [(a, b) for a, b in CONTRACTIONS
                  if re.search(r"\b%s\b" % a.replace("'", "['’]"), base, flags=re.I)]
    pairs = contracted or [(b, a) for a, b in CONTRACTIONS]      # expand what is contracted, else contract
    for src, dst in pairs:
        out = re.sub(r"\b%s\b" % re.escape(src).replace("'", "['’]"), _swap_case(src, dst), out, flags=re.I)
    return out if out != base else None


def edit_bold(base, _req):
    out = base.replace("**", "")
    return out if out != base and not as_json(base) else None


def edit_number(base, req):
    """A number the request gives, changed where the answer repeats it (never a list marker)."""
    given = set(re.findall(r"\d+", req))
    for m in re.finditer(r"\d+", base):
        line_start = base.rfind("\n", 0, m.start()) + 1
        if m.group(0) in given and not re.fullmatch(r"\s*", base[line_start:m.start()]):
            n = m.group(0)
            return base[:m.start()] + str(int(n) + 1).zfill(len(n)) + base[m.end():]
    return None


def _words(s):
    return re.findall(r"\S+", s)


def tense_ok(base, edited):
    """One to four words changed, every number kept: a wording edit, not a content one."""
    ops = difflib.SequenceMatcher(None, _words(base), _words(edited)).get_opcodes()
    changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal")
    return 1 <= changed <= 4 and re.findall(r"\d+", base) == re.findall(r"\d+", edited)


TENSE = ("Change ONE or TWO words only: a verb's tense or aspect (e.g. 'was shipped' -> 'has shipped', 'will "
         "receive' -> 'are going to receive'), or one word for a synonym with exactly the same meaning. Keep every "
         "other character exactly as is.")
DROPFACT = ("Remove exactly one specific fact (a date, a time or an amount) from this text, with the words that only "
            "serve it. Keep every other character exactly as is.")


def edit_enum(base, req):
    """A field whose value is one of the options the request lists ("a"|"b"|"c"), set to the next option."""
    j = as_json(base)
    for opts in re.findall(r'"[^"]+"(?:\s*\|\s*"[^"]+")+', req) if j else []:
        names = re.findall(r'"([^"]+)"', opts)
        for k, v in j[1].items():
            if v in names:
                other = names[(names.index(v) + 1) % len(names)]
                return re.sub(r'("%s"\s*:\s*)"%s"' % (re.escape(k), re.escape(v)), lambda m: m.group(1) + '"%s"'
                              % other, base, count=1)
    return None


def prose_field(base):
    """-> the text of the longest free-text string field of a JSON or tool-call answer, or None."""
    j = as_json(base)
    texts = [v for v in (j[1].values() if j else []) if isinstance(v, str) and len(v.split()) > 3]
    return max(texts, key=len) if texts else None


def with_field(base, old, new):
    """`base` with one string field's text replaced in place, every other byte kept."""
    for ascii_ in (False, True):
        enc = json.dumps(old, ensure_ascii=ascii_)[1:-1]
        if new and enc in base:
            return base.replace(enc, json.dumps(new, ensure_ascii=ascii_)[1:-1], 1)
    return None


def edit_field_contractions(base, _req):
    text = prose_field(base)
    return with_field(base, text, edit_contractions(text, "")) if text else None


def dropfact_ok(text, edited, refs):
    """A number gone, every other kept, and every reference states what went: a fact the set fixes."""
    was, now = collections.Counter(re.findall(r"\d+", text)), collections.Counter(re.findall(r"\d+", edited))
    gone = was - now
    return bool(gone) and not (now - was) and all(n in r for n in gone for r in refs)


CODE_EDITS = {"json": ("within", edit_json), "contractions": ("within", edit_contractions),
              "bold": ("within", edit_bold), "number": ("outside", edit_number),
              "dropkey": ("outside", edit_dropkey), "enum": ("outside", edit_enum),
              "field-contractions": ("within", edit_field_contractions)}

# --- cached model calls ------------------------------------------------------------------------------------------
_lock = threading.Lock()
_cache = None
_spent = [0.0]


def _calls_path():
    return os.path.join(OUT, "calls.jsonl")


def cached(key_parts, call, max_usd=None):
    """-> (raw, cost); (None, 0) when the budget is spent. Paid calls are appended to calls.jsonl as they finish."""
    global _cache
    key = hashlib.sha1(json.dumps(key_parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with _lock:
        if _cache is None:
            _cache = {}
            if os.path.exists(_calls_path()):
                for line in open(_calls_path()):
                    if line.strip():
                        c = json.loads(line)
                        _cache[c["key"]] = c
        if key in _cache:
            return _cache[key]["raw"], 0.0
        if max_usd is not None and _spent[0] >= max_usd:
            return None, 0.0
    raw, cost = call()
    with _lock:
        _spent[0] += cost
        _cache[key] = {"key": key, "raw": raw, "cost": cost}
        with open(_calls_path(), "a") as f:
            f.write(json.dumps(_cache[key], ensure_ascii=False) + "\n")
    return raw, cost


def _spec():
    from app.services.downgrade_refset import spec
    return spec()


def _editor(request, answer, change=TENSE):
    from validation import judges
    sp = _spec()
    system = ("You edit an AI assistant's answer to build a test case. Apply exactly the one change requested and "
              "nothing else. Reply with the edited answer only.")
    msgs = [{"role": "user", "content": "REQUEST:\n%s\n\nANSWER:\n%s\n\nCHANGE: %s" % (request, answer, change)}]
    raw, _ = cached([EDITOR, system, msgs],
                    lambda: judges._complete(dataclasses.replace(sp, judge_model=EDITOR), system, msgs))
    return (raw or "").strip()


# --- cases -------------------------------------------------------------------------------------------------------
def full_view(messages, tools):
    """The whole request as a judge could see it: instructions, tool definitions, every turn in order."""
    parts = []
    for m in messages:
        calls = "".join("\n[calls %s(%s)]" % (c["function"]["name"], c["function"]["arguments"])
                        for c in m.get("tool_calls") or [])
        parts.append("%s:\n%s%s" % ({"system": "SYSTEM", "user": "USER", "assistant": "ASSISTANT",
                                      "tool": "TOOL RESULT"}[m["role"]], m.get("content") or "", calls))
        if m["role"] == "system" and tools:
            parts.append("TOOLS the model could call:\n" + "\n".join(json.dumps(t["function"]) for t in tools))
    return "\n\n".join(parts)


def _originals(pool, sp):
    """STRUCTURED -> {id: (prod view, full view, 6 answers of the original)}."""
    from app.services.audit import replay
    from connectors.bigquery.source import to_trace
    from validation.engine import request_text
    from validation.pricing import cost_of
    built = {}
    for pid, system, user, turns, tools, is_json in STRUCTURED:
        if is_json and "json" not in user:             # OpenAI's JSON mode wants the word in the user turn
            user += " Answer in json."
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}, *turns]
        body = {"model": ORIGINAL, "max_tokens": 600, "messages": msgs, "temperature": 0.3}
        body.update({"tools": tools, **({} if turns else {"tool_choice": "required"})} if tools else {})
        body.update({"response_format": {"type": "json_object"}} if is_json else {})
        trace = to_trace(body, {}, "judge-research")

        def once(n, trace=trace, body=body):
            def call():
                r = replay(trace, ORIGINAL)
                return r["output"], cost_of(ORIGINAL, r.get("usage")) or 0.0
            return cached([ORIGINAL, body, n], call)[0]
        built[pid] = (request_text(trace, sp), full_view(msgs, tools), list(pool.map(once, range(6))))
    return built


def _edits(family, pid, request, full, refs):
    """-> (code-made cases, LLM edit jobs) on the first reference."""
    base, out, jobs = refs[0], [], []
    case = {"family": family, "request": request, "request_full": full, "references": refs, "refs": "sampled"}
    for kind, (expected, fn) in CODE_EDITS.items():
        edited = fn(base, request)
        if edited and edited.strip() != base.strip():
            out.append({**case, "id": "%s/%s" % (pid, kind), "kind": kind, "expected": expected, "candidate": edited})
    field = prose_field(base)
    if not as_json(base):
        jobs.append(({**case, "id": pid + "/tense", "kind": "tense", "expected": "within"}, base, TENSE))
    elif field:
        jobs.append(({**case, "id": pid + "/field-tense", "kind": "field-tense", "expected": "within"}, field, TENSE))
        jobs.append(({**case, "id": pid + "/dropfact", "kind": "dropfact", "expected": "outside"}, field, DROPFACT))
    return out, jobs


def cases(_args):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import judge_eval
    sp = _spec()
    os.makedirs(OUT, exist_ok=True)
    full_of = {p[0]: full_view([{"role": "system", "content": p[2]}, {"role": "user", "content": p[3]}], p[4])
               for p in judge_eval.PROMPTS}
    out, jobs = [], []
    for c in (json.loads(line) for line in open(EVAL_CASES) if line.strip()):
        pid = c["id"].split("/")[0]
        out.append({**c, "family": "eval", "request_full": full_of[pid], "refs": "sampled",
                    "expected": RELABEL.get(c["id"], c["expected"])})
        if c["kind"] == "same-model":
            more, todo = _edits("eval", pid, c["request"], full_of[pid], c["references"])
            out, jobs = out + more, jobs + todo
    with concurrent.futures.ThreadPoolExecutor(POOL) as pool:
        for pid, (request, full, runs) in _originals(pool, sp).items():
            case = {"family": "structured", "request": request, "request_full": full, "references": runs[:5],
                    "refs": "sampled"}
            out.append({**case, "id": pid + "/same-model", "kind": "same-model", "expected": "within",
                        "candidate": runs[5]})
            out += [{**case, "id": "%s/%s" % (pid, kind), "kind": kind, "expected": exp, "candidate": text}
                    for kind, exp, text in REAL.get(pid, [])]
            more, todo = _edits("structured", pid, request, full, runs[:5])
            out, jobs = out + more, jobs + todo
        edited = list(pool.map(lambda j: _editor(j[0]["request"], j[1], j[2]), jobs))
    dropped = []
    for (case, text, change), new in zip(jobs, edited):
        kind, expected = REVIEWED.get(case["id"], (case["kind"], case["expected"])) or (None, None)
        ok = dropfact_ok(text, new, case["references"]) if change == DROPFACT else tense_ok(text, new)
        ok = ok and ("{" in text or "{" not in new)            # the editor sent back the whole JSON, not the field
        cand = new if text == case["references"][0] else with_field(case["references"][0], text, new)
        if kind and ok and cand:
            out.append({**case, "id": "%s/%s" % (case["id"].split("/")[0], kind), "kind": kind,
                        "expected": expected, "candidate": cand})
        else:
            dropped.append(case["id"])
    # frozen: the same edit against the first reference 5 times (its base); same-model / alternative depend on
    # how the sampled references vary, so they stay sampled only
    out += [{**c, "id": c["id"] + "@frozen", "refs": "frozen", "references": [c["references"][0]] * 5}
            for c in out if c["kind"] not in ("same-model", "alternative")]
    with open(os.path.join(OUT, "cases.jsonl"), "w") as f:
        for c in out:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print("%d cases; paid $%.3f; LLM edits dropped (failed the check or by eye): %s"
          % (len(out), _spent[0], ", ".join(dropped) or "none"))
    print(collections.Counter((c["family"], c["refs"], c["expected"]) for c in out))


# --- run ---------------------------------------------------------------------------------------------------------
def variant(name):
    """-> (judge system prompt, which view of the request)."""
    from validation import judges
    return {"J0": (judges.FIT_SYSTEM, "request"), "J0v": (judges.FIT_SYSTEM, "request_full")}[name]


def judge_vote(name, c, vote, sp, max_usd):
    from app.services import llm_client
    from validation import judges
    system, view = variant(name)
    ref = "\n\n".join("--- acceptable %d ---\n%s" % (i + 1, judges.cap(o, sp.ref_chars))
                      for i, o in enumerate(c["references"]))
    prefix = judges.FIT_PREFIX % (c[view][:sp.cap_chars], len(c["references"]), ref)
    msgs = [{"role": "user", "content": [llm_client.cache_block(prefix), {
        "type": "text", "text": "CANDIDATE:\n%s" % judges.cap(c["candidate"], sp.cap_chars)}]}]
    raw, cost = cached([sp.judge_model, system, msgs, vote], lambda: judges._complete(sp, system, msgs), max_usd)
    return (None, "budget spent", cost) if raw is None else (*judges.parse_fit(raw), cost)


def verdict(votes):
    decided = [k for k, _ in votes if k is not None]                   # unsure votes abstain, as in the engine
    kept = sum(1 for k in decided if k)
    if len(decided) * 2 <= len(votes):
        return "unverified"
    return "within" if kept * 2 > len(decided) else "outside"


def run(args):
    sp = _spec()
    all_cases = [json.loads(line) for line in open(os.path.join(OUT, "cases.jsonl")) if line.strip()]
    jobs = [(v, i, n) for v in args.variants for i in range(len(all_cases)) for n in range(sp.votes)]
    with concurrent.futures.ThreadPoolExecutor(POOL) as pool:
        got = list(pool.map(lambda j: judge_vote(j[0], all_cases[j[1]], j[2], sp, args.max_usd), jobs))
    votes = collections.defaultdict(list)
    for (v, i, _), (kept, reason, _) in zip(jobs, got):
        votes[(v, i)].append((kept, reason))
    print("judge %s; paid this run $%.3f\n" % (sp.judge_model, _spent[0]))
    results = []
    for v in args.variants:
        rows = []
        for i, c in enumerate(all_cases):
            vs = votes[(v, i)]
            rows.append({**c, "variant": v, "judged": verdict(vs), "votes": [k for k, _ in vs],
                         "reasons": [w for _, w in vs], "split": len({k for k, _ in vs}) > 1})
        results += rows
        print("== %s" % v)
        for fam, refs, exp in sorted({(r["family"], r["refs"], r["expected"]) for r in rows},
                                     key=lambda k: (k[0], k[1] != "sampled", k[2] != "within")):
            g = [r for r in rows if (r["family"], r["refs"], r["expected"]) == (fam, refs, exp)]
            wrong = sum(1 for r in g if r["judged"] not in (exp, "unverified"))
            unv = sum(1 for r in g if r["judged"] == "unverified")
            print("  %-10s %-7s refs, must %-4s %3d cases: %2d wrong (%s), %d unclear, %d split votes"
                  % (fam, refs, "pass" if exp == "within" else "fail", len(g), wrong,
                     "false drift" if exp == "within" else "false pass", unv, sum(r["split"] for r in g)))
        kinds = collections.defaultdict(lambda: [0, 0])
        for r in rows:
            kinds[(r["kind"], r["refs"])][0] += r["judged"] == r["expected"]
            kinds[(r["kind"], r["refs"])][1] += 1
        print("  by kind: " + ", ".join("%s%s %d/%d" % (k, "@frozen" if f == "frozen" else "", a, n)
                                        for (k, f), (a, n) in sorted(kinds.items())))
        for r in rows:
            if r["judged"] != r["expected"]:
                print("    x %-26s expected %-7s judged %-10s %s" % (r["id"], r["expected"], r["judged"],
                                                                     r["reasons"][0][:110]))
        print()
    path = os.path.join(OUT, "run-%s.json" % "-".join(args.variants))
    json.dump(results, open(path, "w"), indent=1, ensure_ascii=False)
    print("full results -> %s" % path)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cases")
    r = sub.add_parser("run")
    r.add_argument("variants", nargs="+", choices=["J0", "J0v"])
    r.add_argument("--max-usd", type=float, required=True, help="stop paying for new calls past this")
    args = p.parse_args()
    import credentials
    credentials.load()
    {"cases": cases, "run": run}[args.cmd](args)


if __name__ == "__main__":
    main()
