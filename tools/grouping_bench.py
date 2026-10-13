"""Bench: call-site grouping (rounds over word windows, as core/ledger/build.py) and the cache prefix, in BigQuery
SQL on a table whose truth is known. Sandbox only.

    .venv/bin/python tools/grouping_bench.py --make     # build the bench table from the synthetic log's prompts
    .venv/bin/python tools/grouping_bench.py            # run every variant, print accuracy and BigQuery time

The bench table (BENCH) holds calls whose truth is known: the call-site (template) each came from, and the cache
prefix, i.e. how many words of its system prompt come before the first word that changes from call to call. The
templates are real system prompts from the synthetic log; the changing parts are added in plain Python, the hard way:
inside sentences, in one-paragraph prompts, as retrieved blocks of any length, as a date at the very start, plus near-
identical twin templates (must stay apart) and a mid-window revision (must stay together). No LLM.
"""
import argparse
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
                        "true_prefix": prefix, "request": json.dumps({"model": "gpt-5.4", "messages": [
                            {"role": "system", "content": sys_prompt}, {"role": "user", "content": _words(rng, 5, 30)}]})})
    schema = [bigquery.SchemaField(f, t) for f, t in (("id", "STRING"), ("usecase", "STRING"), ("ts", "TIMESTAMP"),
              ("true_site", "STRING"), ("style", "STRING"), ("true_prefix", "INT64"), ("request", "STRING"))]
    job = bq.client().load_table_from_json(out, BENCH, job_config=bigquery.LoadJobConfig(
        schema=schema, write_disposition="WRITE_TRUNCATE"))
    job.result()
    print("%d calls, %d templates, %d usecases -> %s" % (len(out), len(templates) + len(rare), len({t[0] for t in templates}) + 1, BENCH))


# ---- the variants: each fills temp table `found` (id, site, pred_prefix) in a session ---------------------------
_SYS = "COALESCE(JSON_VALUE(request, '$.messages[0].content'), '')"

# prefix the way a provider's cache sees it: each call against the previous call of its call-site, word by word, up
# to the first difference (raw words: a changed number breaks the cache); the call-site's prefix is the median
LCP = """
CREATE TEMP TABLE pairs AS
SELECT site, id, prev FROM (
  SELECT g.site, g.id, LAG(g.id) OVER (PARTITION BY g.site ORDER BY b.ts, g.id) AS prev FROM grp g JOIN `{bench}` b USING (id))
WHERE prev IS NOT NULL QUALIFY ROW_NUMBER() OVER (PARTITION BY site ORDER BY FARM_FINGERPRINT(id)) <= 30;
CREATE TEMP TABLE pw AS
SELECT b.id, i, word FROM `{bench}` b, UNNEST(REGEXP_EXTRACT_ALL(LOWER({sys}), r'[a-z0-9]+')) word WITH OFFSET i
WHERE b.id IN (SELECT id FROM pairs UNION ALL SELECT prev FROM pairs);
CREATE TEMP TABLE lcp AS
SELECT p.site, p.id, COALESCE(MIN(IF(a.word != c.word, a.i, NULL)), COUNT(*)) AS n
FROM pairs p JOIN pw a ON a.id = p.id JOIN pw c ON c.id = p.prev AND c.i = a.i GROUP BY 1, 2;
CREATE TEMP TABLE found AS
SELECT g.id, g.site, m.pref AS pred_prefix
FROM grp g LEFT JOIN (SELECT site, APPROX_QUANTILES(n, 2)[OFFSET(1)] AS pref FROM lcp GROUP BY 1) m USING (site);
"""


def rounds_sql(sample, min_calls, rounds, daily=False):
    """Rounds (peeling), per unit (a usecase, or a usecase-day when `daily`): sample up to `sample` calls not yet
    placed; windows in >= `min_calls` of them are candidate template text; group by the 10 smallest such hashes, merge
    groups whose cores overlap >= 80%; a group's template = windows in >= 90% of its calls (groups of >= min_calls);
    place every unplaced call holding >= 80% of a template (the best one); repeat on the leftovers."""
    unit = ("CONCAT(COALESCE(usecase, ''), '|', CAST(DATE(ts) AS STRING))" if daily else "COALESCE(usecase, '')")
    nxt = ", ' ', ".join(["m"] + ["LEAD(m, %d) OVER w" % j for j in range(1, 5)])
    return """
DECLARE r INT64 DEFAULT 0;
CREATE TEMP TABLE calls AS SELECT id, %(unit)s AS unit, ts, {sys} AS sys FROM `{bench}`;
CREATE TEMP TABLE cw AS
SELECT DISTINCT id, unit, h FROM (
  SELECT id, unit, FARM_FINGERPRINT(CONCAT(%(nxt)s)) AS h
  FROM (SELECT id, unit, i, REGEXP_REPLACE(x, r'[0-9]+', '#') AS m
        FROM calls, UNNEST(REGEXP_EXTRACT_ALL(LOWER(sys), r'[a-z0-9]+')) x WITH OFFSET i)
  QUALIFY MOD(ABS(FARM_FINGERPRINT(m)), 8) = 0 AND LEAD(m, 4) OVER w IS NOT NULL
  WINDOW w AS (PARTITION BY id ORDER BY i));
CREATE TEMP TABLE placed (id STRING, site INT64, round INT64);
CREATE TEMP TABLE templates (unit STRING, site INT64, h INT64);
WHILE r < %(rounds)d DO
  CREATE OR REPLACE TEMP TABLE smp AS
  SELECT c.id FROM calls c LEFT JOIN placed p USING (id) WHERE p.id IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY c.unit ORDER BY FARM_FINGERPRINT(CONCAT(c.id, CAST(r AS STRING)))) <= %(sample)d;
  CREATE OR REPLACE TEMP TABLE common AS
  SELECT w.unit, w.h, COUNT(*) AS n FROM smp JOIN cw w USING (id) GROUP BY 1, 2 HAVING n >= %(min_calls)d;
  CREATE OR REPLACE TEMP TABLE g0 AS
  SELECT w.id, ANY_VALUE(w.unit) AS unit,
         FARM_FINGERPRINT(CONCAT(ANY_VALUE(w.unit), '|', ARRAY_TO_STRING(ARRAY_AGG(CAST(w.h AS STRING) ORDER BY c.n DESC, w.h LIMIT 10), ','))) AS g
  FROM smp JOIN cw w USING (id) JOIN common c ON c.unit = w.unit AND c.h = w.h GROUP BY 1;
  -- within a group, common windows held by 20..80%% of its calls tell templates apart that share most of their text
  -- (twins); calls re-keyed by them; a split by a mere slot value merges back below (its cores overlap)
  CREATE OR REPLACE TEMP TABLE splitter AS
  SELECT g.g, w.h FROM g0 g JOIN (SELECT DISTINCT id, unit, h FROM cw) w USING (id) JOIN common c ON c.unit = w.unit AND c.h = w.h
  JOIN (SELECT g, COUNT(*) AS n FROM g0 GROUP BY 1) k ON k.g = g.g
  GROUP BY 1, 2 HAVING COUNT(*) BETWEEN 0.2 * ANY_VALUE(k.n) AND 0.8 * ANY_VALUE(k.n);
  CREATE OR REPLACE TEMP TABLE g1 AS
  SELECT g.id, ANY_VALUE(g.unit) AS unit,
         FARM_FINGERPRINT(CONCAT(CAST(ANY_VALUE(g.g) AS STRING), '|', ARRAY_TO_STRING(ARRAY_AGG(CAST(s.h AS STRING) ORDER BY s.h LIMIT 10), ','))) AS g
  FROM g0 g LEFT JOIN (SELECT DISTINCT w.id, s.g, s.h FROM cw w JOIN g0 x USING (id) JOIN splitter s ON s.g = x.g AND s.h = w.h) s ON s.id = g.id
  GROUP BY 1;
  CREATE OR REPLACE TEMP TABLE core AS
  SELECT g.unit, g.g, w.h FROM g1 g JOIN cw w USING (id) JOIN common c ON c.unit = w.unit AND c.h = w.h
  JOIN (SELECT g, COUNT(*) AS n FROM g1 GROUP BY 1) k ON k.g = g.g
  WHERE k.n >= %(min_calls)d
  GROUP BY 1, 2, 3 HAVING COUNT(*) >= 0.9 * ANY_VALUE(k.n);
  CREATE OR REPLACE TEMP TABLE hop AS
  WITH sizes AS (SELECT g, COUNT(*) AS n FROM core GROUP BY 1)
  SELECT a, MIN(b) AS b FROM (
    SELECT a.g AS a, b.g AS b FROM core a JOIN core b ON a.unit = b.unit AND a.h = b.h
    JOIN sizes sa ON sa.g = a.g JOIN sizes sb ON sb.g = b.g
    GROUP BY 1, 2 HAVING COUNT(*) / (ANY_VALUE(sa.n) + ANY_VALUE(sb.n) - COUNT(*)) >= 0.8)
  GROUP BY 1;
  CREATE OR REPLACE TEMP TABLE newt AS
  SELECT DISTINCT c.unit, COALESCE(h2.b, h1.b, c.g) AS g, c.h FROM core c LEFT JOIN hop h1 ON h1.a = c.g LEFT JOIN hop h2 ON h2.a = h1.b;
  CREATE OR REPLACE TEMP TABLE tk AS
  SELECT g, ANY_VALUE(unit) AS unit, COUNT(*) AS size,
         FARM_FINGERPRINT(CONCAT(ANY_VALUE(unit), '|', ARRAY_TO_STRING(ARRAY_AGG(CAST(h AS STRING) ORDER BY h LIMIT 10), ','))) AS site
  FROM newt GROUP BY 1;
  INSERT INTO templates SELECT k.unit, k.site, t.h FROM newt t JOIN tk k USING (g);
  INSERT INTO placed
  SELECT id, site, r FROM (
    SELECT w.id, k.site, COUNT(*) / ANY_VALUE(k.size) AS cover
    FROM cw w LEFT JOIN placed p USING (id) JOIN newt t ON t.unit = w.unit AND t.h = w.h JOIN tk k ON k.g = t.g
    WHERE p.id IS NULL GROUP BY 1, 2)
  WHERE cover >= 0.8
  QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY cover DESC, site) = 1;
  SET r = r + 1;
END WHILE;
CREATE TEMP TABLE grp AS
SELECT c.id, COALESCE(p.site, FARM_FINGERPRINT(CONCAT(c.unit, '|not placed'))) AS site FROM calls c LEFT JOIN placed p USING (id);
""" % {"unit": unit, "nxt": nxt, "rounds": rounds, "sample": sample, "min_calls": min_calls} + (
        "CREATE TEMP TABLE found AS SELECT id, site, CAST(NULL AS INT64) AS pred_prefix FROM grp;\n" if daily else LCP)


def link_days(s, min_calls=5, same=0.7):
    """Daily mode: link each day's groups to call-sites across days (template Jaccard >= `same`), then group the calls
    no day placed, across days, the same way. -> {id: final site}, how many stayed unplaced."""
    from collections import Counter, defaultdict
    tmpl = defaultdict(set)
    for r in s.query("SELECT unit, site, h FROM templates"):
        tmpl[(r["unit"], r["site"])].add(r["h"])
    known, link = defaultdict(dict), {}               # usecase -> {site: template}; (unit, day site) -> site
    for (unit, site), t in sorted(tmpl.items(), key=lambda kv: kv[0][0].rsplit("|", 1)[1]):
        uc = unit.rsplit("|", 1)[0]
        best = max(known[uc].items(), key=lambda kv: len(t & kv[1]) / len(t | kv[1]), default=None)
        if best and len(t & best[1]) / len(t | best[1]) >= same:
            link[(unit, site)] = best[0]
            known[uc][best[0]] = t
        else:
            known[uc][site] = t
            link[(unit, site)] = site
    final = {}
    for r in s.query("SELECT p.id, c.unit, p.site FROM placed p JOIN calls c USING (id)"):
        final[r["id"]] = link[(r["unit"], r["site"])]
    left = defaultdict(lambda: defaultdict(set))       # usecase -> id -> windows
    for r in s.query("SELECT w.id, w.unit, w.h FROM cw w LEFT JOIN placed p USING (id) WHERE p.id IS NULL"):
        left[r["unit"].rsplit("|", 1)[0]][r["id"]].add(r["h"])
    unplaced = 0
    for uc, calls in left.items():
        count = Counter(h for ws in calls.values() for h in ws)
        groups = defaultdict(list)
        for i, ws in calls.items():
            sig = tuple(sorted(h for h in ws if count[h] >= min_calls)[:10])
            groups[sig].append(i)
        for sig, ids in groups.items():
            if not sig or len(ids) < min_calls:
                unplaced += len(ids)
                continue
            n = Counter(h for i in ids for h in calls[i])
            t = {h for h, k in n.items() if k >= 0.9 * len(ids)}
            best = max(known[uc].items(), key=lambda kv: len(t & kv[1]) / len(t | kv[1]), default=None)
            site = best[0] if best and len(t & best[1]) / len(t | best[1]) >= same else hash((uc,) + sig)
            known[uc].setdefault(site, t)
            for i in ids:
                if len(calls[i] & t) >= 0.8 * len(t):
                    final[i] = site
                else:
                    unplaced += 1
    return final, unplaced


VARIANTS = [("rounds, sample %d" % n, rounds_sql(n, 5, 3)) for n in (5000, 1000)] + [
    ("rounds daily + linking", rounds_sql(5000, 5, 3, daily=True))]


_SCORE = """
SELECT b.usecase, b.true_site, b.style, f.site, COUNT(*) AS n,
       SUM(ABS(f.pred_prefix - b.true_prefix)) AS abs_err, COUNTIF(ABS(f.pred_prefix - b.true_prefix) <= 5) AS close
FROM found f JOIN `{bench}` b USING (id) GROUP BY 1, 2, 3, 4
"""


def score(rows):
    """Purity: share of a found site's calls from its majority true site. Completeness: share of a true site's calls
    in its majority found site. Both over all calls."""
    from collections import Counter, defaultdict
    found, truth, n = defaultdict(Counter), defaultdict(Counter), 0
    err, close, by_style = 0, 0, defaultdict(lambda: [0, 0, 0])
    for r in rows:
        found[r["site"]][r["true_site"]] += r["n"]
        truth[r["true_site"]][r["site"]] += r["n"]
        n += r["n"]
        err += r["abs_err"]
        close += r["close"]
        s = by_style[r["style"]]
        s[0] += r["n"]; s[1] += r["abs_err"]; s[2] += r["close"]
    purity = sum(max(c.values()) for c in found.values()) / n
    compl = sum(max(c.values()) for c in truth.values()) / n
    return {"sites": len(found), "true": len(truth), "purity": purity, "compl": compl, "mae": err / n,
            "close": close / n, "style": {k: (v[1] / v[0], v[2] / v[0]) for k, v in by_style.items()},
            "split": sorted(t for t, c in truth.items() if max(c.values()) < 0.95 * sum(c.values())),
            "merged": sorted(", ".join(sorted(c)) for c in found.values() if len(c) > 1 and
                             sorted(c.values())[-2] > 0.05 * sum(c.values()))}


def run(only=None):
    from google.cloud import bigquery
    from core.ledger import bq
    os.environ["BQ_TABLE"] = SOURCE
    for name, sql in VARIANTS:
        if only and only not in name:
            continue
        bq._clients.clear()                          # a fresh login per variant: the dev gcloud token lasts an hour
        s = bq.Session()
        try:
            t0 = time.time()
            job = bq.client().query(sql.format(sys=_SYS, bench=BENCH), location=bq.location(), job_config=bigquery.QueryJobConfig(
                job_timeout_ms=300000, connection_properties=[bigquery.ConnectionProperty("session_id", s.id)]))
            try:
                job.result()
            except Exception as e:                   # over the 5-minute cap, or a query error: say so, go on
                print("\n== %s: FAILED after %.0f s: %s" % (name, time.time() - t0, str(e)[:200]), flush=True)
                continue
            secs = time.time() - t0
            slot_s = (job.slot_millis or 0) / 1000
            unplaced = None
            if "daily" in name:
                from collections import Counter
                final, unplaced = link_days(s)
                truth = s.query("SELECT id, usecase, true_site, style FROM `%s`" % BENCH)
                agg = Counter((t["usecase"], t["true_site"], t["style"], final.get(t["id"], "not placed " + t["usecase"])) for t in truth)
                rows = [{"usecase": u, "true_site": ts, "style": st, "site": site, "n": n, "abs_err": 0, "close": 0}
                        for (u, ts, st, site), n in agg.items()]
            else:
                rows = [dict(r) for r in s.query(_SCORE.format(bench=BENCH))]
                if "rounds" in name:
                    unplaced = s.query("SELECT COUNT(*) AS n FROM calls c LEFT JOIN placed p USING (id) WHERE p.id IS NULL")[0]["n"]
            if unplaced is None:                     # calls a method left out of its groups entirely
                total = s.query("SELECT COUNT(*) AS n FROM `%s`" % BENCH)[0]["n"]
                unplaced = total - sum(r["n"] for r in rows)
            sc = score(rows)
        finally:
            s.close()
        print("\n== %s: %.0f s, %.0f slot-s" % (name, secs, slot_s))
        print("   sites found %d (true %d)   purity %.1f%%   completeness %.1f%%   prefix error %.1f words, within 5: %.0f%%"
              % (sc["sites"], sc["true"], 100 * sc["purity"], 100 * sc["compl"], sc["mae"], 100 * sc["close"]))
        print("   prefix by style: " + "  ".join("%s %.1f/%.0f%%" % (k, e, 100 * c) for k, (e, c) in sorted(sc["style"].items())))
        if unplaced is not None:
            print("   not placed: %d calls" % unplaced)
        if sc["split"]:
            print("   split: " + ", ".join(sc["split"]))
        for m in sc["merged"]:
            print("   merged: " + m)
        sys.stdout.flush()


if __name__ == "__main__":
    import credentials
    credentials.load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--make", action="store_true")
    ap.add_argument("--only")
    a = ap.parse_args()
    make() if a.make else run(a.only)
