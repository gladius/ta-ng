"""The daily ledger: one row per usecase x call-site x model x day, built in BigQuery from the log and the call-site
assignment (`call_sites`). Logged cost is the truth; `cost_nocache` is what the same calls would have cost with no
cache reads, from the catalog's prices (NULL for a model the catalog doesn't price)."""
from core.ledger import bq

PRICES_SCHEMA = [("model", "STRING", "REQUIRED"), ("input", "FLOAT", "REQUIRED"), ("output", "FLOAT", "REQUIRED"),
                 ("cache_read", "FLOAT", "REQUIRED"), ("cache_write", "FLOAT", "REQUIRED")]

_SQL = """
CREATE OR REPLACE TABLE {out} PARTITION BY day CLUSTER BY usecase, site AS
WITH calls AS (
  SELECT CAST(l.`{id}` AS STRING) AS id, DATE(l.`{time}`) AS day, l.`{usecase}` AS usecase,
         COALESCE(JSON_VALUE(l.`{resp}`, '$.model'), JSON_VALUE(l.`{req}`, '$.model')) AS model,
         CAST(JSON_VALUE(l.`{resp}`, '$.usage.prompt_tokens') AS INT64) AS prompt_tokens,
         GREATEST(COALESCE(CAST(JSON_VALUE(l.`{resp}`, '$.usage.cache_read_input_tokens') AS INT64), 0),
                  COALESCE(CAST(JSON_VALUE(l.`{resp}`, '$.usage.prompt_tokens_details.cached_tokens') AS INT64), 0)) AS cached,
         COALESCE(CAST(JSON_VALUE(l.`{resp}`, '$.usage.cache_creation_input_tokens') AS INT64), 0) AS written,
         COALESCE(CAST(JSON_VALUE(l.`{resp}`, '$.usage.completion_tokens') AS INT64), 0) AS completion_tokens,
         COALESCE(CAST(JSON_VALUE(l.`{resp}`, '$.usage.cost') AS FLOAT64),
                  CAST(JSON_VALUE(l.txn_metadata, '$.response_cost') AS FLOAT64)) AS cost,
         l.duration_ms,
         ARRAY_LENGTH(JSON_QUERY_ARRAY(l.`{req}`, '$.messages')) AS n_messages,
         JSON_QUERY(l.`{req}`, '$.tools') IS NOT NULL AS has_tools
  FROM `{table}` l
  WHERE DATE(l.`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
    AND JSON_QUERY(l.`{req}`, '$.messages') IS NOT NULL)
SELECT c.day, c.usecase, s.site, c.model, COUNT(*) AS calls,
       SUM(c.prompt_tokens) AS prompt_tokens, SUM(c.cached) AS cached_tokens, SUM(c.written) AS written_tokens,
       SUM(c.completion_tokens) AS completion_tokens,
       COUNTIF(c.cached > 0) AS calls_cached,
       SUM(c.cost) AS cost,
       SUM(c.cost + c.cached * (p.input - p.cache_read) / 1e6) AS cost_nocache,
       AVG(c.duration_ms) AS avg_ms, AVG(c.n_messages) AS avg_messages, COUNTIF(c.has_tools) AS calls_with_tools,
       LOGICAL_OR(s.unsure) AS unsure
FROM calls c
LEFT JOIN {sites} s ON s.id = c.id
LEFT JOIN {prices} p ON p.model = c.model
GROUP BY 1, 2, 3, 4
"""


def prices():
    """The catalog's prices as rows, by canonical model name ($ per 1M tokens)."""
    from app.catalog import PRICE
    return [{"model": m, "input": p["input"], "output": p["output"], "cache_read": p["cache_read"],
             "cache_write": p["cache_write"]} for m, p in PRICE.items()]


def build(days=14):
    """(Re)build `ledger_daily` for the window; returns the row count."""
    c = bq.cfg()
    bq.load_rows("prices", prices(), PRICES_SCHEMA, replace=True)
    bq.query(_SQL.format(out=bq.work("ledger_daily"), sites=bq.work("call_sites"), prices=bq.work("prices"), **c),
             {"days": days})
    return bq.query("SELECT COUNT(*) AS n FROM %s" % bq.work("ledger_daily"))[0]["n"]


def fetch(days=14):
    """The ledger rows for the window, oldest first."""
    return [dict(r) for r in bq.query(
        "SELECT * FROM %s WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY) ORDER BY day, usecase, site, model"
        % bq.work("ledger_daily"), {"days": days})]
