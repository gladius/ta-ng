"""Experiment: how far does a free pass group calls into call-sites, with no model and no learning?

Key per call = the set of tool names + the first PREFIX masked characters of the head (system text, else the first
message with text). Pure SQL, one read. Per usecase: calls, keys, keys of >= MIN calls and the share of calls they
hold (what the free pass places), the rest (what a model would look at). With a truth column: purity of the big keys
and how many keys each true call-site was split into.

    .venv/bin/python tools/free_pass.py --bench               # the hard bench (truth: true_site)
    BQ_TABLE=trust-router-sandbox.litellm.llm_log .venv/bin/python tools/free_pass.py --days 7   # synthetic log
    .venv/bin/python tools/free_pass.py --prod-sql > /tmp/free_pass_prod.sql   # counts-only SQL for the remote session
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ledger import bq, build  # noqa: E402

PREFIX = 300
MIN = 5

_SQL = """
WITH calls AS (
  SELECT COALESCE(`{usecase}`, '') AS usecase, {truth} AS truth,
         FARM_FINGERPRINT(CONCAT(
           ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(t, '$.function.name') FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.tools')) t
                                 WHERE JSON_VALUE(t, '$.function.name') IS NOT NULL ORDER BY 1), ','),
           '|',
           SUBSTR(masked, 1, {prefix}))) AS k,
         FARM_FINGERPRINT(masked) AS whole
  FROM (SELECT *, REGEXP_REPLACE(REGEXP_REPLACE(LOWER({system}), r'[0-9]+', '#'), r'\\s+', ' ') AS masked FROM `{table}`)
  WHERE DATE(`{time}`) BETWEEN @day_from AND @day_to AND JSON_QUERY(`{req}`, '$.messages') IS NOT NULL AND {skip}
),
keys AS (
  SELECT usecase, k, COUNT(*) AS n, MAX(tn) AS top_truth, ANY_VALUE(truth HAVING MAX tn) AS truth, COUNT(DISTINCT whole) = 1 AS static
  FROM (SELECT usecase, k, truth, whole, COUNT(*) OVER (PARTITION BY usecase, k, truth) AS tn FROM calls)
  GROUP BY 1, 2
),
truths AS (
  SELECT usecase, truth, COUNT(DISTINCT k) AS split FROM keys WHERE n >= {min} GROUP BY 1, 2
)
SELECT k.usecase, SUM(n) AS calls, COUNT(*) AS keys,
       COUNTIF(n >= {min}) AS big_keys, SUM(IF(n >= {min}, n, 0)) AS big_calls,
       COUNTIF(n < {min}) AS small_keys, SUM(IF(n < {min}, n, 0)) AS small_calls,
       SUM(IF(n >= {min} AND static, n, 0)) AS static_calls,
       SAFE_DIVIDE(SUM(IF(n >= {min}, top_truth, 0)), SUM(IF(n >= {min}, n, 0))) AS purity,
       (SELECT COUNT(*) FROM truths t WHERE t.usecase = k.usecase) AS true_sites,
       (SELECT SUM(split) FROM truths t WHERE t.usecase = k.usecase) AS big_keys_of_true_sites
FROM keys k GROUP BY 1 ORDER BY calls DESC
"""


def sql(truth, c):
    return _SQL.format(truth=truth, prefix=PREFIX, min=MIN, system=build._SYSTEM.format(text=build._TEXT, head_cap=build.HEAD_CAP, **c),
                       skip=bq.SKIP_FILTER.format(col=c["usecase"]), **c)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--prod-sql", action="store_true", help="print the SQL with the prod column names (no truth), nothing runs")
    a = ap.parse_args()
    if a.prod_sql:
        c = {"table": "PROD_TABLE", "req": "request_payload", "resp": "response_payload", "id": "vegas_common_services_txn_id",
             "usecase": "vegas_usecase_name", "time": "created_timestamp_utc", "latency": "duration_ms"}
        print(sql("'x'", c).replace("@day_from", "DATE 'YYYY-MM-DD'").replace("@day_to", "DATE 'YYYY-MM-DD'").replace("@skip", "['user-%', 'unknown']").replace("@only", "CAST([] AS ARRAY<STRING>)"))
        return
    if a.bench:
        for k, v in (("BQ_TABLE", "trust-router-sandbox.litellm.grouping_bench"), ("BQ_REQUEST_COLUMN", "request"),
                     ("BQ_RESPONSE_COLUMN", "response"), ("BQ_ID_COLUMN", "id"), ("BQ_USECASE_COLUMN", "usecase"),
                     ("BQ_TIME_COLUMN", "ts"), ("BQ_LATENCY_COLUMN", "duration_ms")):
            os.environ[k] = v
        a.days = 15
    c = bq.cfg()
    truth = "true_site" if a.bench else "COALESCE(JSON_VALUE(synthetic, '$.role'), 'main')"
    today = datetime.date.today()
    rows = bq.query(sql(truth, c), {"day_from": today - datetime.timedelta(a.days), "day_to": today - datetime.timedelta(1), **bq.filters()})
    print("%-28s %6s %5s %5s %6s %6s %5s %6s %6s %5s %5s" % ("usecase", "calls", "keys", "big", "placed", "static", "small", "left", "purity", "truth", "split"))
    tot = [0, 0, 0, 0]
    for r in rows:
        tot[0] += r["calls"]; tot[1] += r["big_calls"]; tot[2] += r["small_calls"]; tot[3] += r["static_calls"]
        print("%-28s %6d %5d %5d %5.0f%% %5.0f%% %5d %5.0f%% %5.0f%% %5d %5d" % (
            r["usecase"][:28], r["calls"], r["keys"], r["big_keys"], 100 * r["big_calls"] / r["calls"], 100 * r["static_calls"] / r["calls"], r["small_keys"],
            100 * r["small_calls"] / r["calls"], 100 * (r["purity"] or 0), r["true_sites"] or 0, r["big_keys_of_true_sites"] or 0))
    print("placed by the free pass: %.1f%% of %d calls (%.1f%% in keys whose whole head is static); left for a model: %.1f%%"
          % (100 * tot[1] / tot[0], tot[0], 100 * tot[3] / tot[0], 100 * tot[2] / tot[0]))


if __name__ == "__main__":
    main()
