"""Cache gaps: for every call, the time since the previous call that shared its prefix (same site, model and
system prompt + tools), bucketed, per usecase x site x model x day. The cache module's TTL what-if and the gap
histogram read this; nothing per call leaves BigQuery."""
from core.ledger import bq

# bucket upper bounds in seconds, in order; the TTL options a provider offers must be among them
BUCKETS = [("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600), ("6h", 21600), ("24h", 86400)]

_SQL = """
CREATE OR REPLACE TABLE {out} PARTITION BY day CLUSTER BY usecase, site AS
WITH calls AS (
  SELECT CAST(l.`{id}` AS STRING) AS id, l.`{time}` AS ts, DATE(l.`{time}`) AS day, l.`{usecase}` AS usecase,
         COALESCE(JSON_VALUE(l.`{resp}`, '$.model'), JSON_VALUE(l.`{req}`, '$.model')) AS model,
         FARM_FINGERPRINT(CONCAT(COALESCE(TO_JSON_STRING(JSON_QUERY(l.`{req}`, '$.tools')), ''),
                                 COALESCE(TO_JSON_STRING(JSON_QUERY(l.`{req}`, '$.messages[0].content')), ''))) AS prefix_key,
         COALESCE(CAST(JSON_VALUE(l.`{resp}`, '$.usage.prompt_tokens') AS INT64), 0) AS prompt_tokens,
         GREATEST(COALESCE(CAST(JSON_VALUE(l.`{resp}`, '$.usage.cache_read_input_tokens') AS INT64), 0),
                  COALESCE(CAST(JSON_VALUE(l.`{resp}`, '$.usage.prompt_tokens_details.cached_tokens') AS INT64), 0)) AS cached,
         COALESCE(CAST(JSON_VALUE(l.`{resp}`, '$.usage.cache_creation_input_tokens') AS INT64), 0) AS written
  FROM `{table}` l
  WHERE DATE(l.`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
    AND JSON_QUERY(l.`{req}`, '$.messages') IS NOT NULL),
seq AS (
  SELECT c.*, s.site,
         TIMESTAMP_DIFF(c.ts, LAG(c.ts) OVER w, SECOND) AS gap,
         MIN(c.prompt_tokens) OVER (PARTITION BY c.usecase, s.site, c.model, c.prefix_key) AS prefix_tokens
  FROM calls c LEFT JOIN {sites} s ON s.id = c.id AND s.day = c.day
  WINDOW w AS (PARTITION BY c.usecase, s.site, c.model, c.prefix_key ORDER BY c.ts))
SELECT day, usecase, CAST(site AS STRING) AS site, model,
       CASE WHEN gap IS NULL THEN 'first' {cases} ELSE 'more' END AS bucket,
       COUNT(*) AS calls, SUM(prefix_tokens) AS prefix_tokens, SUM(prompt_tokens) AS prompt_tokens,
       SUM(cached) AS cached, SUM(written) AS written
FROM seq GROUP BY 1, 2, 3, 4, 5
"""


def build(days=14):
    c = bq.cfg()
    cases = " ".join("WHEN gap <= %d THEN '%s'" % (secs, name) for name, secs in BUCKETS)
    bq.query(_SQL.format(out=bq.work("cache_gaps"), sites=bq.work("call_sites"), cases=cases, **c), {"days": days})
    return bq.query("SELECT COUNT(*) AS n FROM %s" % bq.work("cache_gaps"))[0]["n"]


def fetch(days=14):
    return [dict(r) for r in bq.query(
        "SELECT * FROM %s WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)" % bq.work("cache_gaps"),
        {"days": days})]
