"""The refresh script: one BigQuery session, temporary tables only, results into the app's database.

Call-site identity (core idea, scored exact on the synthetic log by tools/identity_score.py): a call-site is one
prompt template. Split each system prompt into lines, mask numbers, and count in what share of the usecase's prompts
each line appears. A STABLE line is in >= STABLE of them; a TEMPLATE line in >= TEMPLATE. A prompt's signature is
its TOP_K longest stable lines of MIN_LEN+ characters (instructions, not `Plan: Gold`); a prompt with none belongs
to a minority template and uses its template lines. The hash of the signature is the call-site id. Per-call context,
retrieved passages and short slot lines never reach it, wherever they sit. A version is the set of long template
lines the prompt holds that are stable or live for a limited run of days (a revision, not a repeating slot value).

Temp tables the script leaves in the session: site_lines, call_sites, sites_daily, site_layout, ledger_daily,
cache_gaps. The fetch_* functions read them; nothing is written to the customer's dataset.
"""
from core.ledger import bq

MIN_LEN = 40
STABLE = 0.5
TEMPLATE = 0.05
TOP_K = 10
SAMPLE = 5000           # prompts per usecase (and per site) the shares are counted on, inside BigQuery
BUCKETS = [("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600), ("6h", 21600), ("24h", 86400)]

_LINES = """ARRAY(SELECT TRIM(REGEXP_REPLACE(x, r'[0-9]+', '#')) FROM UNNEST(SPLIT(
  COALESCE(JSON_VALUE(`{req}`, '$.messages[0].content'),
           ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(p, '$.text') FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.messages[0].content')) p), '\\n'),
           ''), '\\n')) x)"""

SCRIPT = """
-- 1. the usecases' line shares, from a bounded sample per usecase
CREATE TEMP TABLE site_lines AS
WITH prompts AS (
  SELECT `{usecase}` AS usecase, DATE(`{time}`) AS day,
         ARRAY(SELECT DISTINCT l FROM UNNEST({lines}) l WHERE LENGTH(l) >= 8) AS lines
  FROM `{table}`
  WHERE DATE(`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY) AND JSON_QUERY(`{req}`, '$.messages') IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY `{usecase}` ORDER BY FARM_FINGERPRINT(CAST(`{id}` AS STRING))) <= {sample})
SELECT r.usecase, l AS line, COUNT(*) / ANY_VALUE(t.total) AS share,
       COUNT(DISTINCT r.day) < 0.8 * ANY_VALUE(t.days) AS limited_days
FROM prompts r, UNNEST(r.lines) l
JOIN (SELECT usecase, COUNT(*) AS total, COUNT(DISTINCT day) AS days FROM prompts GROUP BY 1) t ON t.usecase IS NOT DISTINCT FROM r.usecase
GROUP BY 1, 2 HAVING share >= {template};

-- 2. every call -> site and version
CREATE TEMP TABLE call_rows AS
WITH prompts AS (
  SELECT CAST(`{id}` AS STRING) AS id, `{usecase}` AS usecase, DATE(`{time}`) AS day, `{time}` AS ts,
         COALESCE(JSON_VALUE(`{resp}`, '$.model'), JSON_VALUE(`{req}`, '$.model')) AS model,
         FARM_FINGERPRINT(COALESCE(TO_JSON_STRING(ARRAY(SELECT JSON_VALUE(t, '$.function.name')
                                   FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.tools')) t ORDER BY 1)), '')) AS tools_hash,
         FARM_FINGERPRINT(CONCAT(COALESCE(TO_JSON_STRING(JSON_QUERY(`{req}`, '$.tools')), ''),
                                 COALESCE(TO_JSON_STRING(JSON_QUERY(`{req}`, '$.messages[0].content')), ''))) AS prefix_key,
         COALESCE(CAST(JSON_VALUE(`{resp}`, '$.usage.prompt_tokens') AS INT64), 0) AS prompt_tokens,
         GREATEST(COALESCE(CAST(JSON_VALUE(`{resp}`, '$.usage.cache_read_input_tokens') AS INT64), 0),
                  COALESCE(CAST(JSON_VALUE(`{resp}`, '$.usage.prompt_tokens_details.cached_tokens') AS INT64), 0)) AS cached,
         COALESCE(CAST(JSON_VALUE(`{resp}`, '$.usage.cache_creation_input_tokens') AS INT64), 0) AS written,
         COALESCE(CAST(JSON_VALUE(`{resp}`, '$.usage.completion_tokens') AS INT64), 0) AS completion_tokens,
         CAST(JSON_VALUE(`{resp}`, '$.usage.cost') AS FLOAT64) AS cost,
         CAST(`{latency}` AS FLOAT64) AS duration_ms,
         ARRAY_LENGTH(JSON_QUERY_ARRAY(`{req}`, '$.messages')) AS n_messages,
         JSON_QUERY(`{req}`, '$.tools') IS NOT NULL AS has_tools,
         {lines} AS lines
  FROM `{table}`
  WHERE DATE(`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY) AND JSON_QUERY(`{req}`, '$.messages') IS NOT NULL),
tagged AS (
  SELECT r.id, l AS line, LENGTH(l) AS chars,
         d.share IS NOT NULL AND LENGTH(l) >= {min_len} AS template,
         COALESCE(d.share, 0) >= {stable} AND LENGTH(l) >= {min_len} AS stable,
         d.share IS NOT NULL AND LENGTH(l) >= {min_len} AND (d.share >= {stable} OR d.limited_days) AS versioning
  FROM prompts r, UNNEST(r.lines) l
  LEFT JOIN site_lines d ON d.usecase IS NOT DISTINCT FROM r.usecase AND d.line = l
  WHERE LENGTH(l) >= 8),
per_row AS (
  SELECT id,
         ARRAY_AGG(IF(stable, line, NULL) IGNORE NULLS ORDER BY chars DESC, line LIMIT {top_k}) AS stable_lines,
         ARRAY_AGG(IF(template, line, NULL) IGNORE NULLS ORDER BY chars DESC, line LIMIT {top_k}) AS template_lines,
         ARRAY_AGG(IF(versioning, line, NULL) IGNORE NULLS ORDER BY line) AS version_lines
  FROM tagged GROUP BY 1),
sig AS (SELECT id, IF(ARRAY_LENGTH(stable_lines) > 0, stable_lines, template_lines) AS signature, version_lines FROM per_row)
SELECT p.* EXCEPT (lines),
       FARM_FINGERPRINT(CONCAT(COALESCE(p.usecase, ''), '\\x1f', ARRAY_TO_STRING(ARRAY(SELECT x FROM UNNEST(s.signature) x ORDER BY x), '\\n'))) AS site,
       FARM_FINGERPRINT(COALESCE(ARRAY_TO_STRING(s.version_lines, '\\n'), '')) AS version,
       ARRAY_TO_STRING(ARRAY(SELECT x FROM UNNEST(s.signature) x ORDER BY x), '\\n') AS signature_text
FROM prompts p LEFT JOIN sig s USING (id);

CREATE TEMP TABLE sites_daily AS
SELECT usecase, site, day, COUNT(*) AS n, ANY_VALUE(signature_text) AS signature, ARRAY_AGG(DISTINCT version IGNORE NULLS) AS versions
FROM call_rows GROUP BY 1, 2, 3;

-- 3. per site: fixed vs per-call text, from a sample per site
CREATE TEMP TABLE sample_rows AS
SELECT c.site, c.tools_hash, FARM_FINGERPRINT(c.id) AS rid, {lines} AS lines
FROM `{table}` l JOIN call_rows c ON c.id = CAST(l.`{id}` AS STRING) AND c.day = DATE(l.`{time}`)
WHERE DATE(l.`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
QUALIFY ROW_NUMBER() OVER (PARTITION BY c.site ORDER BY FARM_FINGERPRINT(c.id)) <= {sample};
CREATE TEMP TABLE tagged_s AS
SELECT site, rid, off, LENGTH(l) AS chars, l AS line FROM sample_rows, UNNEST(lines) l WITH OFFSET off WHERE LENGTH(l) >= 8;
CREATE TEMP TABLE fixed AS
SELECT t.site, t.line FROM tagged_s t JOIN (SELECT site, COUNT(DISTINCT rid) AS n FROM tagged_s GROUP BY 1) s USING (site)
GROUP BY 1, 2, s.n HAVING COUNT(DISTINCT rid) >= {stable} * s.n;
CREATE TEMP TABLE per_row_s AS
SELECT t.site, t.rid, SUM(IF(f.line IS NOT NULL, chars, 0)) AS static_chars, SUM(IF(f.line IS NULL, chars, 0)) AS dynamic_chars,
       MIN(IF(f.line IS NULL, off, NULL)) AS first_dynamic
FROM tagged_s t LEFT JOIN fixed f ON f.site = t.site AND f.line = t.line GROUP BY 1, 2;
CREATE TEMP TABLE site_layout AS
SELECT p.site, AVG(p.static_chars) AS static_chars, AVG(p.dynamic_chars) AS dynamic_chars, AVG(COALESCE(a.static_after, 0)) AS static_after,
       ANY_VALUE(ts.tool_sets) AS tool_sets
FROM per_row_s p
LEFT JOIN (SELECT t.site, t.rid, SUM(t.chars) AS static_after FROM tagged_s t JOIN fixed f ON f.site = t.site AND f.line = t.line
           JOIN per_row_s q ON q.site = t.site AND q.rid = t.rid WHERE t.off > COALESCE(q.first_dynamic, 1000000000) GROUP BY 1, 2) a
  ON a.site = p.site AND a.rid = p.rid
LEFT JOIN (SELECT site, COUNT(DISTINCT tools_hash) AS tool_sets FROM sample_rows GROUP BY 1) ts ON ts.site = p.site
GROUP BY 1;

-- 4. the ledger: usecase x site x model x day
CREATE TEMP TABLE prices AS SELECT * FROM UNNEST(@prices);
CREATE TEMP TABLE ledger_daily AS
SELECT c.day, c.usecase, c.site, c.model, COUNT(*) AS calls,
       SUM(c.prompt_tokens) AS prompt_tokens, SUM(c.cached) AS cached_tokens, SUM(c.written) AS written_tokens,
       SUM(c.completion_tokens) AS completion_tokens, COUNTIF(c.cached > 0) AS calls_cached,
       SUM(c.cost) AS cost, SUM(c.cost + c.cached * (p.input - p.cache_read) / 1e6) AS cost_nocache,
       AVG(c.duration_ms) AS avg_ms, AVG(c.n_messages) AS avg_messages, COUNTIF(c.has_tools) AS calls_with_tools
FROM call_rows c LEFT JOIN prices p ON p.model = c.model
GROUP BY 1, 2, 3, 4;

-- 5. gaps: time since the previous call sharing the prefix, bucketed
CREATE TEMP TABLE cache_gaps AS
WITH seq AS (
  SELECT c.*, TIMESTAMP_DIFF(c.ts, LAG(c.ts) OVER w, SECOND) AS gap,
         MIN(c.prompt_tokens) OVER (PARTITION BY c.usecase, c.site, c.model, c.prefix_key) AS prefix_tokens
  FROM call_rows c WINDOW w AS (PARTITION BY c.usecase, c.site, c.model, c.prefix_key ORDER BY c.ts))
SELECT day, usecase, site, model, CASE WHEN gap IS NULL THEN 'first' {cases} ELSE 'more' END AS bucket,
       COUNT(*) AS calls, SUM(prefix_tokens) AS prefix_tokens, SUM(prompt_tokens) AS prompt_tokens,
       SUM(cached) AS cached, SUM(written) AS written
FROM seq GROUP BY 1, 2, 3, 4, 5;
"""

_SITES = """
WITH vers AS (
  SELECT usecase, site, ARRAY_AGG(STRUCT(version, first, last) ORDER BY first) AS versions FROM (
    SELECT usecase, site, v AS version, MIN(day) AS first, MAX(day) AS last FROM sites_daily, UNNEST(versions) v GROUP BY 1, 2, 3)
  GROUP BY 1, 2)
SELECT s.usecase, CAST(s.site AS STRING) AS site, SUM(n) AS n, ANY_VALUE(signature HAVING MAX day) AS signature,
       MIN(day) AS first_day, MAX(day) AS last_day, ANY_VALUE(v.versions) AS versions,
       ANY_VALUE(y.static_chars) AS static_chars, ANY_VALUE(y.dynamic_chars) AS dynamic_chars,
       ANY_VALUE(y.static_after) AS static_after, ANY_VALUE(y.tool_sets) AS tool_sets
FROM sites_daily s JOIN vers v ON v.usecase IS NOT DISTINCT FROM s.usecase AND v.site = s.site
LEFT JOIN site_layout y ON y.site = s.site
GROUP BY 1, 2
"""
_USECASE_LINES = "SELECT usecase, line, share, limited_days FROM site_lines WHERE share >= %s" % STABLE
_LEDGER = "SELECT * EXCEPT (site), CAST(site AS STRING) AS site FROM ledger_daily"
_GAPS = "SELECT * EXCEPT (site), CAST(site AS STRING) AS site FROM cache_gaps"
_SAMPLE = """
SELECT l.`{req}` AS request FROM `{table}` l JOIN call_rows c ON c.id = CAST(l.`{id}` AS STRING) AND c.day = DATE(l.`{time}`)
WHERE c.site = @site LIMIT 1
"""


def _names():
    c = bq.cfg()
    return {**c, "lines": _LINES.format(**c), "min_len": MIN_LEN, "stable": STABLE, "template": TEMPLATE,
            "top_k": TOP_K, "sample": SAMPLE,
            "cases": " ".join("WHEN gap <= %d THEN '%s'" % (secs, name) for name, secs in BUCKETS)}


def _prices():
    from app.catalog import PRICE
    return [{"model": m, "input": float(p["input"]), "cache_read": float(p["cache_read"])} for m, p in PRICE.items()]


class Build:
    """Run the script once, then read its results."""
    def __init__(self, days=30):
        self.days = days
        self.s = bq.Session()
        self.s.query(SCRIPT.format(**_names()), {"days": days, "prices": _prices()})

    def sites(self):
        return [dict(r) for r in self.s.query(_SITES)]

    def usecase_lines(self):
        return [dict(r) for r in self.s.query(_USECASE_LINES)]

    def ledger(self):
        return [dict(r) for r in self.s.query(_LEDGER)]

    def gaps(self):
        return [dict(r) for r in self.s.query(_GAPS)]

    def sample_request(self, site):
        import json
        rows = self.s.query(_SAMPLE.format(**_names()), {"site": int(site)})
        if not rows:
            return None
        r = rows[0]["request"]
        return json.loads(r) if isinstance(r, str) else r

    def close(self):
        self.s.close()
