"""Score the grouping (the day script's placement plus the loop) on a table whose true call-site is known.

    BQ_TABLE=trust-router-sandbox.litellm.llm_log .venv/bin/python tools/identity_score.py [--days 7]
    .venv/bin/python tools/identity_score.py --bench            # the hard bench (tools/grouping_bench.py --make)
    .venv/bin/python tools/identity_score.py --bench --prepass  # with the BigQuery pre-pass (core/ledger/bq_minhash.py)

Paid: the loop asks the model once per call-site (about 2 cents each). Runs the day script over the last --days days
at once (one BigQuery session) with nothing known, runs the loop, and scores `placed` against the truth: purity =
share of a found site's calls from its majority true site; completeness = share of a true site's calls in its majority
found site. The app's database is not touched (the loop's records are kept in memory)."""
import argparse
import datetime
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.identity import loop, store  # noqa: E402
from core.ledger import build, bq  # noqa: E402

_SQL = """
SELECT s.usecase, p.site, {truth} AS truth, COUNT(*) AS n
FROM placed p JOIN src s USING (id) JOIN `{table}` l ON CAST(l.`{id}` AS STRING) = s.log_id AND UNIX_MICROS(CAST(l.`{time}` AS TIMESTAMP)) = s.ts_us
WHERE s.chat GROUP BY 1, 2, 3
"""
_LEFT = "SELECT s.usecase, COUNT(*) AS n FROM calls s LEFT JOIN placed p USING (id) WHERE p.id IS NULL GROUP BY 1"


def main(days, bench):
    if bench:
        for k, v in (("BQ_TABLE", "trust-router-sandbox.litellm.grouping_bench"), ("BQ_REQUEST_COLUMN", "request"),
                     ("BQ_RESPONSE_COLUMN", "response"), ("BQ_ID_COLUMN", "id"), ("BQ_USECASE_COLUMN", "usecase"),
                     ("BQ_TIME_COLUMN", "ts"), ("BQ_LATENCY_COLUMN", "duration_ms")):
            os.environ[k] = v
        days = 15
    c = bq.cfg()
    truth = "l.true_site" if bench else "COALESCE(JSON_VALUE(l.synthetic, '$.role'), 'main')"
    today = datetime.date.today()
    made = {}
    store.save_site = lambda rec: made.__setitem__(rec["site"], rec)       # in memory, not the app's database
    from core import tasks
    tasks.note = lambda text: print("  ..", text, flush=True)
    b = build.Build(today - datetime.timedelta(days), today - datetime.timedelta(1))
    try:
        loop.run(b, str(today - datetime.timedelta(1)), lambda uc: [{"site": s, "name": r["profile"]["name"], "phrases": r["phrases"], "calls": r["placed"]}
                                                                    for s, r in made.items() if (r["usecase"] or "") == (uc or "")])
        rows = b.s.query(_SQL.format(truth=truth, **c))
        left = {r["usecase"] or "": r["n"] for r in b.s.query(_LEFT)}
    finally:
        b.close()
    by = {uc: [] for uc in left}
    for r in rows:
        by.setdefault(r["usecase"] or "", []).append(r)
    print("%-24s %5s %7s %7s %6s %6s %8s" % ("usecase", "sites", "truth", "purity", "compl", "calls", "unplaced"))
    for uc in sorted(by, key=lambda u: -sum(r["n"] for r in by[u])):
        found, truth = {}, Counter()
        for r in by[uc]:
            found.setdefault(r["site"], Counter())[r["truth"]] += r["n"]
            truth[r["truth"]] += r["n"]
        n = sum(truth.values())
        purity = sum(max(cnt.values()) for cnt in found.values()) / n if n else 0
        compl = sum(max(cnt[t] for cnt in found.values()) for t in truth) / n if n else 0
        print("%-24s %5d %7d %5.0f%% %5.0f%% %6d %8d" % (uc, len(found), len(truth), 100 * purity, 100 * compl, n, left.get(uc, 0)))
    for s, r in made.items():
        name = r["profile"]["name"] or ("[%s]" % r["profile"]["model"])
        print("  %-28s %-24s %5d %s %s" % ((r["usecase"] or "")[:28], name[:24], r["placed"],
                                           "MIXED" if r.get("mixed") else "     ", " | ".join(r["phrases"])[:100]))


if __name__ == "__main__":
    import credentials
    credentials.load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--prepass", action="store_true", help="GROUPING_PREPASS=1: the BigQuery pre-pass before the loop")
    a = ap.parse_args()
    if a.prepass:
        os.environ["GROUPING_PREPASS"] = "1"
    main(a.days, a.bench)
