"""Score the day script's grouping on the synthetic log, whose `synthetic` column holds the true call-site.

    BQ_TABLE=trust-router-sandbox.litellm.llm_log .venv/bin/python tools/identity_score.py [--days 7]

Runs the day script over the last --days days at once (one BigQuery session) and scores call_rows against the truth:
the orchestrator's roles are distinct call-sites; every other usecase is one. Purity = share of a found site's calls
from its majority true site; completeness = share of a true site's calls in its majority found site. Sites found per
day are linked across days the way the app does (core.ledger.refresh.link)."""
import argparse
import datetime
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ledger import build, bq, refresh  # noqa: E402

_SQL = """
SELECT a.usecase, CAST(a.site AS STRING) AS site, COALESCE(JSON_VALUE(l.synthetic, '$.role'), 'main') AS truth, COUNT(*) AS n
FROM call_rows a JOIN `{table}` l ON CAST(l.`{id}` AS STRING) = a.id AND a.day = DATE(l.`{time}`)
GROUP BY 1, 2, 3
"""
_LEFT = "SELECT s.usecase, COUNT(*) AS n FROM src s LEFT JOIN placed p USING (id) WHERE p.id IS NULL GROUP BY 1"


def main(days):
    c = bq.cfg()
    today = datetime.date.today()
    b = build.Build(today - datetime.timedelta(days), today - datetime.timedelta(1))
    try:
        known, link = {}, {}
        for d in sorted(b.day_sites(), key=lambda d: (str(d["day"]), d["site"])):
            k = known.setdefault(d["usecase"] or "", {})
            t = {int(h) for h in d["template"]}
            site = refresh.link(t, k) or refresh.site_id(d["usecase"], t)
            k[site] = t
            link[d["site"]] = site
        rows = b.s.query(_SQL.format(**c))
        left = {r["usecase"] or "": r["n"] for r in b.s.query(_LEFT)}
    finally:
        b.close()
    by = {}
    for r in rows:
        by.setdefault(r["usecase"] or "", []).append(r)
    print("%-24s %5s %7s %7s %6s %6s %8s" % ("usecase", "sites", "truth", "purity", "compl", "calls", "unplaced"))
    for uc in sorted(by, key=lambda u: -sum(r["n"] for r in by[u])):
        found, truth = {}, Counter()
        for r in by[uc]:
            found.setdefault(link[r["site"]], Counter())[r["truth"]] += r["n"]
            truth[r["truth"]] += r["n"]
        n = sum(truth.values())
        purity = sum(max(cnt.values()) for cnt in found.values()) / n
        compl = sum(max(cnt[t] for cnt in found.values()) for t in truth) / n
        print("%-24s %5d %7d %5.0f%% %5.0f%% %6d %8d" % (uc, len(found), len(truth), 100 * purity, 100 * compl, n, left.get(uc, 0)))


if __name__ == "__main__":
    import credentials
    credentials.load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    main(ap.parse_args().days)
