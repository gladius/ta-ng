"""Score core.identity's grouping on the synthetic log, whose `synthetic` column holds the true call-site.

    BQ_TABLE=trust-router-sandbox.litellm.llm_log BQ_AGENT_COLUMN=vegas_usecase_name BQ_ID_COLUMN=vegas_common_services_txn_id \\
      .venv/bin/python tools/identity_score.py [--days 14] [--build]

--build computes the line shares and assigns every day of the window not built yet. Truth: the orchestrator's roles are distinct
call-sites; every other usecase is one call-site (its versions are versions of it). Purity = share of a found site's
rows that belong to its majority true site; completeness = share of a true site's rows in its majority found site.
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.identity import sql as identity  # noqa: E402
from core.ledger import bq  # noqa: E402

_SQL = """
SELECT a.usecase, a.site, FALSE AS unsure,
       COALESCE(JSON_VALUE(l.synthetic, '$.role'), 'main') AS truth, COUNT(*) AS n
FROM {assign} a JOIN `{table}` l ON CAST(l.`{id}` AS STRING) = a.id AND a.day = DATE(l.`{time}`)
GROUP BY 1, 2, 3, 4
"""
_TOTAL = """
SELECT `{usecase}` AS usecase, COUNT(*) AS n FROM `{table}`
WHERE DATE(`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY) AND JSON_QUERY(`{req}`, '$.messages') IS NOT NULL
GROUP BY 1
"""


def main(days, build):
    if build:
        import datetime
        identity.build_lines(days)
        done = identity.built_days()
        identity.assign_days([d for d in identity.log_days(days) if str(d) not in done])
        identity.build_layout(days)
        print("%d sites" % len(identity.sites(days)))
    c = bq.cfg()
    rows = bq.query(_SQL.format(assign=bq.work("call_sites"), **c))
    totals = {r["usecase"]: r["n"] for r in bq.query(_TOTAL.format(**c), {"days": days})}
    by = {}
    for r in rows:
        by.setdefault(r["usecase"], []).append(r)
    print("%-24s %5s %7s %8s %7s %6s %6s" % ("usecase", "sites", "truth", "assigned", "purity", "compl", "unsure"))
    for uc in sorted(by, key=lambda u: -totals.get(u, 0)):
        rs = by[uc]
        found = {}
        truth = Counter()
        for r in rs:
            found.setdefault(r["site"], Counter())[r["truth"]] += r["n"]
            truth[r["truth"]] += r["n"]
        assigned = sum(truth.values())
        purity = sum(max(cnt.values()) for cnt in found.values()) / assigned
        # completeness: for each true site, its rows in the found site holding most of them
        compl = sum(max(cnt[t] for cnt in found.values()) for t in truth) / assigned
        unsure = sum(r["n"] for r in rs if r["unsure"]) / assigned
        print("%-24s %5d %7d %7d%% %6.0f%% %5.0f%% %5.0f%%" % (uc, len(found), len(truth),
              round(100 * assigned / totals.get(uc, assigned)), 100 * purity, 100 * compl, 100 * unsure))
    missing = [u for u in totals if u not in by]
    if missing:
        print("no rows assigned:", missing)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--build", action="store_true")
    a = ap.parse_args()
    main(a.days, a.build)
