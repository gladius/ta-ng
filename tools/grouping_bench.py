"""The hard bench for call-site grouping: a sandbox table of calls whose call-site is known, scored by
tools/identity_score.py --bench with the app's own day script.

    .venv/bin/python tools/grouping_bench.py            # build the bench table from the synthetic log's prompts

The bench table (BENCH) holds calls whose truth is known: the call-site (template) each came from, and the cache
prefix, i.e. how many words of its system prompt come before the first word that changes from call to call. The
templates are real system prompts from the synthetic log; the changing parts are added in plain Python, the hard way:
inside sentences, in one-paragraph prompts, as retrieved blocks of any length, as a date at the very start, plus near-
identical twin templates (must stay apart) and a mid-window revision (must stay together). No LLM.
"""
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOURCE = "trust-router-sandbox.litellm.llm_log"
BENCH = "trust-router-sandbox.litellm.grouping_bench"
CALLS = 300                      # per template
DAYS = 14
WORDS = re.compile(r"[a-z0-9]+")  # the same tokens as BigQuery's REGEXP_EXTRACT_ALL(LOWER(t), r'[a-z0-9]+')

_POOL = ("account billing refund invoice router modem outage claim policy device signal coverage premium deductible "
         "warranty shipment delivery address priority escalation balance payment schedule technician appointment "
         "region tower roaming upgrade discount contract renewal fraud verification identity document receipt "
         "customer agent supervisor ticket summary status pending approved rejected review notes history").split()
_NAMES = "maria jose li wei ahmed khan olga petrova john smith ana lucia van der berg priya nair kim tran".split()
_PLANS = ["gold", "silver", "bronze", "unlimited plus", "family share", "business pro"]


def tokens(text):
    return WORDS.findall(text.lower())


def _words(rng, lo, hi):
    return " ".join(rng.choice(_POOL) for _ in range(rng.randint(lo, hi)))


def _sentence_break(text, frac):
    """The index just after a sentence end nearest to `frac` of the text (inside a line, not at a line end)."""
    target = int(len(text) * frac)
    cands = [m.end() for m in re.finditer(r"[.!?] (?=[A-Z])", text)]
    return min(cands, key=lambda i: abs(i - target)) if cands else target


def _line_break(text, frac):
    target = int(len(text) * frac)
    cands = [m.end() for m in re.finditer(r"\n", text)]
    return min(cands, key=lambda i: abs(i - target)) if cands else len(text)


def variant(base, style, rng, day):
    """(system prompt, true prefix in words) for one call of a template in a given style."""
    name = " ".join(rng.choice(_NAMES) for _ in range(rng.randint(1, 4)))
    plan = rng.choice(_PLANS)
    if style == "inline":            # the changing part inside a sentence, 3 to 30 words
        i = _sentence_break(base, 0.3)
        ins = "You are helping %s, %s, on plan %s. " % (name, _words(rng, 2, 25), plan)
        return base[:i] + ins + base[i:], len(tokens(base[:i]))
    if style == "paragraph":         # no line breaks at all, the changing part in the middle
        flat = re.sub(r"\s*\n\s*", " ", base)
        i = _sentence_break(flat, 0.5)
        ins = "The customer is %s on plan %s, %s. " % (name, plan, _words(rng, 2, 25))
        return flat[:i] + ins + flat[i:], len(tokens(flat[:i]))
    if style == "context":           # a retrieved block of 20 to 300 words on its own lines
        i = _line_break(base, 0.6)
        ws = _words(rng, 20, 300).split()
        block = "\n".join(" ".join(ws[j:j + 12]) for j in range(0, len(ws), 12))
        return base[:i] + "Relevant notes:\n" + block + "\n" + base[i:], len(tokens(base[:i])) + 2
    if style == "date_first":        # a date and time at the very start: nothing is cacheable
        d = time.gmtime(time.time() - rng.randint(0, DAYS * 86400))
        return time.strftime("Today is %A, %d %B %Y, %H:%M.\n", d) + base, 2
    if style == "lines":             # the easy case the line method was built for
        i = _line_break(base, 0.4)
        return base[:i] + "Customer: %s\nPlan: %s\n" % (name, plan) + base[i:], len(tokens(base[:i])) + 1
    raise ValueError(style)


def make():
    from google.cloud import bigquery
    from core.ledger import bq
    os.environ["BQ_TABLE"] = SOURCE
    c = bq.cfg()
    rows = bq.query("""
        SELECT `{usecase}` AS usecase, COALESCE(JSON_VALUE(synthetic, '$.role'), 'main') AS role,
               ANY_VALUE(JSON_VALUE(`{req}`, '$.messages[0].content')) AS sys
        FROM `{table}` WHERE JSON_QUERY(`{req}`, '$.messages') IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2""".format(**c))
    bases = {(r["usecase"], r["role"]): r["sys"] for r in rows if r["usecase"] and len(tokens(r["sys"] or "")) > 100}
    styles = ["inline", "paragraph", "context", "date_first", "lines"]
    templates = []                   # (bench usecase, true site, base text, style, revision text or None)
    for n, ((uc, role), text) in enumerate(sorted(bases.items())):
        templates.append((uc, "%s/%s" % (uc, role), text, styles[n % len(styles)], None))
    # twins: two call-sites of one usecase sharing their first 70% (must stay two)
    a, b = bases[("uc-assistant-platform", "main")], bases[("uc-plan-advisor", "main")]
    cut_a, cut_b = _line_break(a, 0.7), _line_break(b, 0.7)
    templates.append(("bench-twins", "twins/1", a, "inline", None))
    templates.append(("bench-twins", "twins/2", a[:cut_a] + b[cut_b:], "inline", None))
    # three one-paragraph call-sites in one usecase (must be three)
    for role in ("uc-kb-answer", "uc-ticket-summary", "uc-risk-score"):
        templates.append(("bench-paragraphs", "paragraphs/" + role, bases[(role, "main")], "paragraph", None))
    # a revision mid-window: one rule added, one changed (must stay one call-site)
    e = bases[("uc-email-draft", "main")]
    i = _line_break(e, 0.5)
    revised = e[:i] + "Never promise a delivery date unless the order system confirms it.\n" + e[i:].replace(" the ", " this ", 1)
    templates.append(("bench-revision", "revision/1", e, "inline", revised))
    # rare call-sites: one busy call-site, one of ~150 calls (3.6%) and one of 15 (0.4%) in the same usecase
    rare = [("bench-rare", "rare/main", bases[("uc-order-triage", "main")], "inline", None, 4000),
            ("bench-rare", "rare/150", bases[("uc-outage-notice", "main")], "context", None, 150),
            ("bench-rare", "rare/15", bases[("uc-device-troubleshoot", "main")], "lines", None, 15)]
    rng, out, n = random.Random(7), [], 0
    for uc, site, text, style, revised, calls in [t + (CALLS,) for t in templates] + rare:
        for k in range(calls):
            day = rng.randint(0, DAYS - 1)
            base = revised if revised and day >= DAYS // 2 else text
            sys_prompt, prefix = variant(base, style, rng, day)
            n += 1
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - day * 86400 - rng.randint(0, 86399)))
            out.append({"id": "b%06d" % n, "usecase": uc, "ts": ts, "true_site": site, "style": style,
                        "true_prefix": prefix, "response": None, "duration_ms": None, "request": json.dumps({"model": "gpt-5.4", "messages": [
                            {"role": "system", "content": sys_prompt}, {"role": "user", "content": _words(rng, 5, 30)}]})})
    schema = [bigquery.SchemaField(f, t) for f, t in (("id", "STRING"), ("usecase", "STRING"), ("ts", "TIMESTAMP"),
              ("true_site", "STRING"), ("style", "STRING"), ("true_prefix", "INT64"), ("request", "STRING"),
              ("response", "STRING"), ("duration_ms", "FLOAT"))]
    job = bq.client().load_table_from_json(out, BENCH, job_config=bigquery.LoadJobConfig(
        schema=schema, write_disposition="WRITE_TRUNCATE"))
    job.result()
    print("%d calls, %d templates, %d usecases -> %s" % (len(out), len(templates) + len(rare), len({t[0] for t in templates}) + 1, BENCH))




if __name__ == "__main__":
    import credentials
    credentials.load()
    make()
