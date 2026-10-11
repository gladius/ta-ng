"""Call-site assignment in BigQuery: identity.describe() on a sample of each usecase's prompts gives anchors; the
`sites` table holds them; `call_sites` assigns every row of the window to the first site whose anchors it contains."""
import json

from core.identity import skeleton
from core.ledger import bq

SAMPLE = 2000
SITES_SCHEMA = [("usecase", "STRING", "NULLABLE"), ("site", "STRING", "REQUIRED"), ("rank", "INTEGER", "REQUIRED"),
                ("n", "INTEGER", "REQUIRED"), ("unsure", "BOOL", "REQUIRED"), ("anchors", "STRING", "REPEATED"),
                ("static_lines", "INTEGER", "REQUIRED"), ("dynamic_lines", "INTEGER", "REQUIRED"),
                ("static_chars", "INTEGER", "REQUIRED"), ("dynamic_chars", "INTEGER", "REQUIRED"),
                ("static_after_dynamic", "FLOAT", "REQUIRED"), ("tool_sets", "INTEGER", "REQUIRED"),
                ("versions", "STRING", "NULLABLE")]

_SAMPLE_SQL = """
SELECT `{usecase}` AS usecase, `{time}` AS ts,
       JSON_QUERY(`{req}`, '$.messages[0]') AS first,
       ARRAY(SELECT JSON_VALUE(t, '$.function.name') FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.tools')) t) AS tools
FROM `{table}`
WHERE DATE(`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
  AND JSON_QUERY(`{req}`, '$.messages') IS NOT NULL
QUALIFY ROW_NUMBER() OVER (PARTITION BY `{usecase}` ORDER BY FARM_FINGERPRINT(CAST(`{id}` AS STRING))) <= @n
"""

_ASSIGN_SQL = """
CREATE OR REPLACE TABLE {out}
PARTITION BY day CLUSTER BY usecase, site AS
SELECT id, usecase, day, model, site, unsure FROM (
  SELECT r.id, r.usecase, r.day, r.model, s.site, s.unsure,
         ROW_NUMBER() OVER (PARTITION BY r.id ORDER BY s.rank) AS pick
  FROM (
    SELECT CAST(`{id}` AS STRING) AS id, `{usecase}` AS usecase, DATE(`{time}`) AS day,
           COALESCE(JSON_VALUE(`{resp}`, '$.model'), JSON_VALUE(`{req}`, '$.model')) AS model,
           COALESCE(JSON_VALUE(`{req}`, '$.messages[0].content'),
                    ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(p, '$.text')
                                          FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.messages[0].content')) p), '\\n')) AS first
    FROM `{table}`
    WHERE DATE(`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
      AND JSON_QUERY(`{req}`, '$.messages') IS NOT NULL) r
  JOIN {sites} s ON s.usecase IS NOT DISTINCT FROM r.usecase
  WHERE ARRAY_LENGTH(s.anchors) > 0
    AND (SELECT LOGICAL_AND(STRPOS(REGEXP_REPLACE(r.first, r'[0-9]+', '#'), a) > 0) FROM UNNEST(s.anchors) a)
) WHERE pick = 1
"""


def build(days=14, sample=SAMPLE):
    """Sample, group, write `sites`, assign -> `call_sites`. Returns the sites written."""
    c = bq.cfg()
    rows = bq.query(_SAMPLE_SQL.format(**c), {"days": days, "n": sample})
    by = {}
    for r in rows:
        by.setdefault(r["usecase"], []).append((bq.system_text(r["first"]), list(r["tools"] or []), r["ts"]))
    out = []
    for usecase, items in by.items():
        prompts = [(p, t) for p, t, _ in items]
        for rank, d in enumerate(skeleton.describe(usecase, prompts, [ts for _, _, ts in items])):
            out.append({"usecase": usecase, "site": d["site"], "rank": rank, "n": d["n"], "unsure": d["unsure"],
                        "anchors": d["anchors"], "static_lines": d["static"], "dynamic_lines": d["dynamic"],
                        **d["layout"], "versions": json.dumps(d["versions"])})
    bq.load_rows("sites", out, SITES_SCHEMA, replace=True)
    bq.query(_ASSIGN_SQL.format(out=bq.work("call_sites"), sites=bq.work("sites"), **c), {"days": days})
    return out
