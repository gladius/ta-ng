"""Call-site identity, computed in BigQuery and never downloaded.

A call-site is one prompt template. The log has no id for it, so one is derived from the lines a usecase's prompts
share: split the system prompt into lines, mask numbers, keep lines of MIN_LEN+ characters (instructions, not
`Plan: Gold`), and count in what share of the usecase's prompts each line appears. A STABLE line is in >= STABLE of
them; a TEMPLATE line in >= TEMPLATE. A prompt's signature is its TOP_K longest stable lines (a prompt with none
belongs to a minority template: its template lines instead); the hash of that signature is the call-site id. Per-call
context, retrieved passages and short slot lines never reach the signature, wherever they sit in the prompt. A
version is the set of template lines the prompt holds: a revision line (in a few % of calls) changes the version,
not the site. Scored exact on the synthetic log (tools/identity_score.py).

Tables, next to the log (bq.work):
  site_lines   usecase, line, share                 the lines and their shares, from a bounded sample per usecase
  call_sites   id, usecase, day, model, site, version, layout numbers      one row per call, built one day at a time
  sites_daily  usecase, site, day, n, signature, versions                 per site and day, from the same query
  site_layout  site, static/dynamic characters, static after the first per-call line, tool sets   from a sample per site
"""
from core.ledger import bq

MIN_LEN = 40
STABLE = 0.5
TEMPLATE = 0.05
TOP_K = 10
SAMPLE = 5000           # prompts per usecase the line shares are counted on (inside BigQuery)

_LINES = """ARRAY(SELECT TRIM(REGEXP_REPLACE(x, r'[0-9]+', '#')) FROM UNNEST(SPLIT(
  COALESCE(JSON_VALUE(`{req}`, '$.messages[0].content'),
           ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(p, '$.text') FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.messages[0].content')) p), '\\n'),
           ''), '\\n')) x)"""

_SITE_LINES = """
CREATE OR REPLACE TABLE {lines_t} CLUSTER BY usecase AS
WITH prompts AS (
  SELECT `{usecase}` AS usecase, DATE(`{time}`) AS day,
         ARRAY(SELECT DISTINCT l FROM UNNEST({lines}) l WHERE LENGTH(l) >= 8) AS lines
  FROM `{table}`
  WHERE DATE(`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY) AND JSON_QUERY(`{req}`, '$.messages') IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY `{usecase}` ORDER BY FARM_FINGERPRINT(CAST(`{id}` AS STRING))) <= {sample})
SELECT r.usecase, l AS line, COUNT(*) / ANY_VALUE(t.total) AS share,
       -- a revision line is live for a run of days; a slot value that repeats is spread over all of them
       COUNT(DISTINCT r.day) < 0.8 * ANY_VALUE(t.days) AS limited_days
FROM prompts r, UNNEST(r.lines) l
JOIN (SELECT usecase, COUNT(*) AS total, COUNT(DISTINCT day) AS days FROM prompts GROUP BY 1) t ON t.usecase IS NOT DISTINCT FROM r.usecase
GROUP BY 1, 2 HAVING share >= {template}
"""

_ASSIGN = """
CREATE TEMP TABLE day_rows AS
WITH prompts AS (
  SELECT CAST(`{id}` AS STRING) AS id, `{usecase}` AS usecase, DATE(`{time}`) AS day,
         COALESCE(JSON_VALUE(`{resp}`, '$.model'), JSON_VALUE(`{req}`, '$.model')) AS model,
         FARM_FINGERPRINT(COALESCE(TO_JSON_STRING(ARRAY(SELECT JSON_VALUE(t, '$.function.name')
                                   FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.tools')) t ORDER BY 1)), '')) AS tools_hash,
         {lines} AS lines
  FROM `{table}`
  WHERE DATE(`{time}`) IN UNNEST(@days) AND JSON_QUERY(`{req}`, '$.messages') IS NOT NULL),
tagged AS (
  SELECT r.id, r.usecase, r.day, r.model, r.tools_hash, off, l AS line, LENGTH(l) AS chars,
         d.share IS NOT NULL AND LENGTH(l) >= {min_len} AS template,          -- signature material: long template lines
         COALESCE(d.share, 0) >= {stable} AND LENGTH(l) >= {min_len} AS stable,
         d.share IS NOT NULL AND LENGTH(l) >= {min_len} AND (d.share >= {stable} OR d.limited_days) AS versioning
  FROM prompts r, UNNEST(r.lines) l WITH OFFSET off
  LEFT JOIN {lines_t} d ON d.usecase IS NOT DISTINCT FROM r.usecase AND d.line = l
  WHERE LENGTH(l) >= 8),
per_row AS (
  SELECT id, usecase, day, model, tools_hash,
         ARRAY_AGG(IF(stable, line, NULL) IGNORE NULLS ORDER BY chars DESC, line LIMIT {top_k}) AS stable_lines,
         ARRAY_AGG(IF(template, line, NULL) IGNORE NULLS ORDER BY chars DESC, line LIMIT {top_k}) AS template_lines,
         ARRAY_AGG(IF(versioning, line, NULL) IGNORE NULLS ORDER BY line) AS version_lines,
  FROM tagged GROUP BY 1, 2, 3, 4, 5),
sig AS (
  SELECT p.*, IF(ARRAY_LENGTH(stable_lines) > 0, stable_lines, template_lines) AS signature FROM per_row p)
SELECT id, usecase, day, model, tools_hash,
       FARM_FINGERPRINT(CONCAT(COALESCE(usecase, ''), '\\x1f', ARRAY_TO_STRING(ARRAY(SELECT x FROM UNNEST(signature) x ORDER BY x), '\\n'))) AS site,
       FARM_FINGERPRINT(COALESCE(ARRAY_TO_STRING(version_lines, '\\n'), '')) AS version,
       ARRAY_TO_STRING(ARRAY(SELECT x FROM UNNEST(signature) x ORDER BY x), '\\n') AS signature_text
FROM sig;

DELETE FROM {calls_t} WHERE day IN UNNEST(@days);
INSERT INTO {calls_t} (id, usecase, day, model, site, version, tools_hash)
SELECT id, usecase, day, model, site, version, tools_hash FROM day_rows;

DELETE FROM {sites_t} WHERE day IN UNNEST(@days);
INSERT INTO {sites_t} (usecase, site, day, n, signature, versions)
SELECT usecase, site, day, COUNT(*), ANY_VALUE(signature_text), ARRAY_AGG(DISTINCT version IGNORE NULLS)
FROM day_rows GROUP BY 1, 2, 3;
"""

_CREATE_CALLS = """CREATE TABLE IF NOT EXISTS {calls_t} (id STRING, usecase STRING, day DATE, model STRING, site INT64,
  version INT64, tools_hash INT64)
  PARTITION BY day CLUSTER BY usecase, site"""
_LAYOUT = """
CREATE TEMP TABLE sample_rows AS
SELECT c.site, c.tools_hash, FARM_FINGERPRINT(c.id) AS rid, {lines} AS lines
FROM `{table}` l JOIN {calls_t} c ON c.id = CAST(l.`{id}` AS STRING) AND c.day = DATE(l.`{time}`)
WHERE DATE(l.`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
QUALIFY ROW_NUMBER() OVER (PARTITION BY c.site ORDER BY FARM_FINGERPRINT(c.id)) <= {sample};

CREATE TEMP TABLE tagged AS
SELECT site, rid, off, LENGTH(l) AS chars, l AS line FROM sample_rows, UNNEST(lines) l WITH OFFSET off WHERE LENGTH(l) >= 8;

CREATE TEMP TABLE fixed AS                       -- a line is fixed for the SITE when most of its calls carry it
SELECT t.site, t.line FROM tagged t JOIN (SELECT site, COUNT(DISTINCT rid) AS n FROM tagged GROUP BY 1) s USING (site)
GROUP BY 1, 2, s.n HAVING COUNT(DISTINCT rid) >= {stable} * s.n;

CREATE TEMP TABLE per_row AS
SELECT t.site, t.rid, SUM(IF(f.line IS NOT NULL, chars, 0)) AS static_chars, SUM(IF(f.line IS NULL, chars, 0)) AS dynamic_chars,
       MIN(IF(f.line IS NULL, off, NULL)) AS first_dynamic
FROM tagged t LEFT JOIN fixed f ON f.site = t.site AND f.line = t.line GROUP BY 1, 2;

CREATE OR REPLACE TABLE {layout_t} AS
SELECT p.site, AVG(p.static_chars) AS static_chars, AVG(p.dynamic_chars) AS dynamic_chars, AVG(COALESCE(a.static_after, 0)) AS static_after,
       ANY_VALUE(ts.tool_sets) AS tool_sets
FROM per_row p
LEFT JOIN (SELECT t.site, t.rid, SUM(t.chars) AS static_after FROM tagged t JOIN fixed f ON f.site = t.site AND f.line = t.line
           JOIN per_row q ON q.site = t.site AND q.rid = t.rid WHERE t.off > COALESCE(q.first_dynamic, 1000000000) GROUP BY 1, 2) a
  ON a.site = p.site AND a.rid = p.rid
LEFT JOIN (SELECT site, COUNT(DISTINCT tools_hash) AS tool_sets FROM sample_rows GROUP BY 1) ts ON ts.site = p.site
GROUP BY 1
"""

_CREATE_SITES = """CREATE TABLE IF NOT EXISTS {sites_t} (usecase STRING, site INT64, day DATE, n INT64, signature STRING,
  static_chars FLOAT64, dynamic_chars FLOAT64, static_after FLOAT64, tool_sets INT64, versions ARRAY<INT64>)
  PARTITION BY day CLUSTER BY usecase, site"""

_SITES = """
WITH vers AS (
  SELECT usecase, site, ARRAY_AGG(STRUCT(version, first, last) ORDER BY first) AS versions FROM (
    SELECT usecase, site, v AS version, MIN(day) AS first, MAX(day) AS last
    FROM {sites_t}, UNNEST(versions) v WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY) GROUP BY 1, 2, 3)
  GROUP BY 1, 2)
SELECT s.usecase, CAST(s.site AS STRING) AS site, SUM(n) AS n, ANY_VALUE(signature HAVING MAX day) AS signature,
       MIN(day) AS first_day, MAX(day) AS last_day, ANY_VALUE(v.versions) AS versions,
       ANY_VALUE(y.static_chars) AS static_chars, ANY_VALUE(y.dynamic_chars) AS dynamic_chars,
       ANY_VALUE(y.static_after) AS static_after, ANY_VALUE(y.tool_sets) AS tool_sets
FROM {sites_t} s JOIN vers v ON v.usecase IS NOT DISTINCT FROM s.usecase AND v.site = s.site
LEFT JOIN {layout_t} y ON y.site = s.site
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
GROUP BY 1, 2
"""



def _names():
    c = bq.cfg()
    return {**c, "lines": _LINES.format(**c), "lines_t": bq.work("site_lines"), "calls_t": bq.work("call_sites"),
            "sites_t": bq.work("sites_daily"), "layout_t": bq.work("site_layout"), "min_len": MIN_LEN, "stable": STABLE, "template": TEMPLATE,
            "top_k": TOP_K, "sample": SAMPLE}


def build_lines(days=30):
    """The line shares per usecase, from a sample of the window. Rebuild when prompts may have changed (weekly)."""
    bq.query(_SITE_LINES.format(**_names()), {"days": days})


def built_days():
    n = _names()
    bq.query(_CREATE_CALLS.format(**n))
    bq.query(_CREATE_SITES.format(**n))
    return {str(r["day"]) for r in bq.query("SELECT DISTINCT day FROM %s" % n["calls_t"])}


def log_days(days=30):
    """The days of the window that have chat rows (partition column only: cheap)."""
    c = bq.cfg()
    return [r["day"] for r in bq.query("SELECT DISTINCT DATE(`%s`) AS day FROM `%s` WHERE DATE(`%s`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY) ORDER BY 1"
                                        % (c["time"], c["table"], c["time"]), {"days": days})]


def assign_days(days):
    """Every call of these days -> its site and version, plus the per-site summary per day; one job. Re-runnable."""
    if days:
        bq.query(_ASSIGN.format(**_names()), {"days": list(days)})


def build_layout(days=30):
    """Per site: fixed vs per-call characters and where the per-call part starts, from a sample per site."""
    bq.query(_LAYOUT.format(**_names()), {"days": days})


def sites(days=30):
    """The call-sites of the window with their signature, layout numbers and versions."""
    return [dict(r) for r in bq.query(_SITES.format(**_names()), {"days": days})]


_SAMPLE = """
SELECT l.`{req}` AS request FROM `{table}` l JOIN {calls_t} c ON c.id = CAST(l.`{id}` AS STRING) AND c.day = DATE(l.`{time}`)
WHERE c.site = @site AND DATE(l.`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY) LIMIT 1
"""


def sample_request(site, days=30):
    """One logged request of the site (the profiler reads it whole), as a dict, or None."""
    import json
    rows = bq.query(_SAMPLE.format(**_names()), {"site": int(site), "days": days})
    if not rows:
        return None
    r = rows[0]["request"]
    return json.loads(r) if isinstance(r, str) else r
