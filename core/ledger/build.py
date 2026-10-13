"""The day script: one BigQuery session, temporary tables only, for a range of days and every usecase at once. Its
results land in the app's database (core/ledger/refresh.py links each day's call-sites to the known ones).

The log is read once (temp table src). BigQuery bills the columns it reads for every day in the range, whatever the
usecase filter (it has no index), so a run covers all usecases: one usecase alone would cost the same.

Call-sites, per usecase and day (a unit), in rounds: sample up to SAMPLE calls of the unit not placed yet; cut their
system prompts into windows of WINDOW words (numbers masked, kept only at anchor words: 1 in ANCHOR, chosen by the
word's own hash, so every prompt keeps the same windows); windows in >= MIN_CALLS sampled calls are candidate template
text; group calls by their TOP_K most common windows and merge groups whose cores overlap >= SAME; a group of >= MIN_CALLS
calls has a template: the windows in >= CORE of its calls. Every unplaced call holding >= CONTAIN of a template is placed
(the best one), and the next round samples what is left, where a rare call-site is common. Calls no round places are
returned with their windows: the app groups those across days. Names, dates and retrieved passages of any length don't
matter, wherever they sit; there is no share threshold to hide a small call-site.

Cache layout per day-site: the prefix as a provider's cache sees it (each call against the previous one of its
call-site, word by word, raw: a changed number breaks it; PAIRS pairs, the median); fixed vs changing text from windows
as written. Scored by tools/grouping_bench.py: 24/24 call-sites, including ones of 150 and 15 calls among 4,000.
"""
from core.ledger import bq

WINDOW = 5
ANCHOR = 8
MIN_CALLS = 5
CORE = 0.9
SAME = 0.8
CONTAIN = 0.8
TOP_K = 10
SAMPLE = 5000           # calls per unit and round the groups are learned on
ROUNDS = 3
PAIRS = 30              # consecutive-call pairs per day-site for the prefix
POOL = 5                # request ids kept per day-site and model for model audits (plus the extremes)
LEFTOVER = 500          # unplaced calls returned per usecase and day
BUCKETS = [("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600), ("6h", 21600), ("24h", 86400)]

_SYSTEM = """COALESCE(JSON_VALUE(`{req}`, '$.messages[0].content'),
           ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(p, '$.text') FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.messages[0].content')) p), '\\n'),
           '')"""

SCRIPT = """
DECLARE r INT64 DEFAULT 0;

-- 1. the log, read once: every chat call of the days, its numbers and its system prompt
CREATE TEMP TABLE src AS
SELECT CAST(`{id}` AS STRING) AS id, COALESCE(`{usecase}`, '') AS usecase, DATE(`{time}`) AS day, `{time}` AS ts,
       COALESCE(JSON_VALUE(`{resp}`, '$.model'), JSON_VALUE(`{req}`, '$.model')) AS model,
       FARM_FINGERPRINT(COALESCE(TO_JSON_STRING(ARRAY(SELECT JSON_VALUE(t, '$.function.name')
                                 FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.tools')) t ORDER BY 1)), '')) AS tools_hash,
       ARRAY(SELECT JSON_VALUE(t, '$.function.name') FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.tools')) t) AS tool_names,
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
       {system} AS sys,
       SUBSTR(JSON_VALUE(`{req}`, '$.messages[1].content'), 1, 2000) AS first_user
FROM `{table}`
WHERE DATE(`{time}`) BETWEEN @day_from AND @day_to AND JSON_QUERY(`{req}`, '$.messages') IS NOT NULL;

-- 2. word windows at anchor words: masked (h: grouping) and as written (r: what the cache sees); one row per word
CREATE TEMP TABLE cw AS
SELECT DISTINCT id, unit, h, r FROM (
  SELECT id, unit, FARM_FINGERPRINT(CONCAT({window})) AS h, FARM_FINGERPRINT(CONCAT({raw_window})) AS r
  FROM (SELECT id, CONCAT(usecase, '\\x1f', CAST(day AS STRING)) AS unit, i, x, REGEXP_REPLACE(x, r'[0-9]+', '#') AS m
        FROM src, UNNEST(REGEXP_EXTRACT_ALL(LOWER(sys), r'[a-z0-9]+')) x WITH OFFSET i)
  QUALIFY MOD(ABS(FARM_FINGERPRINT(m)), {anchor}) = 0 AND LEAD(m, {last}) OVER w IS NOT NULL
  WINDOW w AS (PARTITION BY id ORDER BY i));

-- 3. rounds: learn groups on a sample of the unplaced calls, place every call that holds a template, repeat
CREATE TEMP TABLE placed (id STRING, site INT64);
CREATE TEMP TABLE templates (unit STRING, site INT64, h INT64);
WHILE r < {rounds} DO
  CREATE OR REPLACE TEMP TABLE smp AS
  SELECT id FROM (SELECT DISTINCT id, unit FROM cw) c LEFT JOIN placed p USING (id) WHERE p.id IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY c.unit ORDER BY FARM_FINGERPRINT(CONCAT(c.id, CAST(r AS STRING)))) <= {sample};
  CREATE OR REPLACE TEMP TABLE common AS
  SELECT w.unit, w.h, COUNT(DISTINCT w.id) AS n FROM smp JOIN cw w USING (id) GROUP BY 1, 2 HAVING n >= {min_calls};
  -- a call's key: its TOP_K most common windows (the histogram's top), so calls of one template share a key
  CREATE OR REPLACE TEMP TABLE g0 AS
  SELECT id, ANY_VALUE(unit) AS unit,
         FARM_FINGERPRINT(CONCAT(ANY_VALUE(unit), '|', ARRAY_TO_STRING(ARRAY_AGG(CAST(h AS STRING) ORDER BY n DESC, h LIMIT {top_k}), ','))) AS g
  FROM (SELECT DISTINCT w.id, w.unit, w.h, c.n FROM smp JOIN cw w USING (id) JOIN common c ON c.unit = w.unit AND c.h = w.h)
  GROUP BY 1;
  -- within a group, common windows held by 20..80% of its calls tell templates apart that share most of their text
  -- (twins); calls re-keyed by them; a split by a mere slot value merges back below (its cores overlap)
  CREATE OR REPLACE TEMP TABLE splitter AS
  SELECT g.g, w.h FROM g0 g JOIN (SELECT DISTINCT id, unit, h FROM cw) w USING (id) JOIN common c ON c.unit = w.unit AND c.h = w.h
  JOIN (SELECT g, COUNT(*) AS n FROM g0 GROUP BY 1) k ON k.g = g.g
  GROUP BY 1, 2 HAVING COUNT(*) BETWEEN 0.2 * ANY_VALUE(k.n) AND 0.8 * ANY_VALUE(k.n);
  CREATE OR REPLACE TEMP TABLE g1 AS
  SELECT g.id, ANY_VALUE(g.unit) AS unit,
         FARM_FINGERPRINT(CONCAT(CAST(ANY_VALUE(g.g) AS STRING), '|', ARRAY_TO_STRING(ARRAY_AGG(CAST(s.h AS STRING) ORDER BY s.h LIMIT {top_k}), ','))) AS g
  FROM g0 g LEFT JOIN (SELECT DISTINCT w.id, s.g, s.h FROM cw w JOIN g0 x USING (id) JOIN splitter s ON s.g = x.g AND s.h = w.h) s ON s.id = g.id
  GROUP BY 1;
  CREATE OR REPLACE TEMP TABLE core AS
  SELECT g.unit, g.g, w.h FROM g1 g JOIN (SELECT DISTINCT id, unit, h FROM cw) w USING (id)
  JOIN common c ON c.unit = w.unit AND c.h = w.h
  JOIN (SELECT g, COUNT(*) AS n FROM g1 GROUP BY 1) k ON k.g = g.g
  WHERE k.n >= {min_calls}
  GROUP BY 1, 2, 3 HAVING COUNT(*) >= {core} * ANY_VALUE(k.n);
  CREATE OR REPLACE TEMP TABLE hop AS
  WITH sizes AS (SELECT g, COUNT(*) AS n FROM core GROUP BY 1)
  SELECT a, MIN(b) AS b FROM (
    SELECT a.g AS a, b.g AS b FROM core a JOIN core b ON a.unit = b.unit AND a.h = b.h
    JOIN sizes sa ON sa.g = a.g JOIN sizes sb ON sb.g = b.g
    GROUP BY 1, 2 HAVING COUNT(*) / (ANY_VALUE(sa.n) + ANY_VALUE(sb.n) - COUNT(*)) >= {same})
  GROUP BY 1;
  CREATE OR REPLACE TEMP TABLE newt AS
  SELECT DISTINCT c.unit, COALESCE(h2.b, h1.b, c.g) AS g, c.h FROM core c LEFT JOIN hop h1 ON h1.a = c.g LEFT JOIN hop h2 ON h2.a = h1.b;
  CREATE OR REPLACE TEMP TABLE tk AS
  SELECT g, ANY_VALUE(unit) AS unit, COUNT(*) AS size,
         FARM_FINGERPRINT(CONCAT(ANY_VALUE(unit), '|', ARRAY_TO_STRING(ARRAY_AGG(CAST(h AS STRING) ORDER BY CAST(h AS STRING) LIMIT {top_k}), ','))) AS site
  FROM newt GROUP BY 1;
  INSERT INTO templates SELECT k.unit, k.site, t.h FROM newt t JOIN tk k USING (g);
  INSERT INTO placed
  SELECT id, site FROM (
    SELECT w.id, k.site, COUNT(*) / ANY_VALUE(k.size) AS cover
    FROM (SELECT DISTINCT id, unit, h FROM cw) w LEFT JOIN placed p USING (id)
    JOIN newt t ON t.unit = w.unit AND t.h = w.h JOIN tk k ON k.g = t.g
    WHERE p.id IS NULL GROUP BY 1, 2)
  WHERE cover >= {contain}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY cover DESC, site) = 1;
  SET r = r + 1;
END WHILE;

CREATE TEMP TABLE call_rows AS SELECT s.* EXCEPT (sys, first_user, tool_names), p.site FROM src s JOIN placed p USING (id);

-- 4. per day-site: the prompt's layout for the cache
CREATE TEMP TABLE pairs AS
SELECT site, id, prev FROM (
  SELECT c.site, c.id, LAG(c.id) OVER (PARTITION BY c.site ORDER BY c.ts, c.id) AS prev FROM call_rows c)
WHERE prev IS NOT NULL QUALIFY ROW_NUMBER() OVER (PARTITION BY site ORDER BY FARM_FINGERPRINT(id)) <= {pairs};
CREATE TEMP TABLE lcp AS
WITH pw AS (SELECT s.id, i, x FROM src s, UNNEST(REGEXP_EXTRACT_ALL(LOWER(s.sys), r'[a-z0-9]+')) x WITH OFFSET i
            WHERE s.id IN (SELECT id FROM pairs UNION ALL SELECT prev FROM pairs))
SELECT p.site, p.id, COALESCE(MIN(IF(a.x != b.x, a.i, NULL)), COUNT(*)) AS n
FROM pairs p JOIN pw a ON a.id = p.id JOIN pw b ON b.id = p.prev AND b.i = a.i GROUP BY 1, 2;
CREATE TEMP TABLE site_layout AS
WITH smp AS (SELECT site, id FROM call_rows QUALIFY ROW_NUMBER() OVER (PARTITION BY site ORDER BY FARM_FINGERPRINT(id)) <= {sample}),
  wr AS (SELECT DISTINCT id, r FROM cw),
  fixedr AS (SELECT p.site, w.r FROM smp p JOIN wr w USING (id)
             JOIN (SELECT site, COUNT(*) AS n FROM smp GROUP BY 1) k ON k.site = p.site
             GROUP BY 1, 2 HAVING COUNT(*) >= 0.5 * ANY_VALUE(k.n)),
  fx AS (SELECT p.site, p.id, COUNTIF(f.r IS NOT NULL) / COUNT(*) AS fixed_share
         FROM smp p JOIN wr w USING (id) LEFT JOIN fixedr f ON f.site = p.site AND f.r = w.r GROUP BY 1, 2),
  agg AS (SELECT p.site, AVG(LENGTH(s.sys)) AS chars, AVG(ARRAY_LENGTH(REGEXP_EXTRACT_ALL(LOWER(s.sys), r'[a-z0-9]+'))) AS words,
                 AVG(COALESCE(fx.fixed_share, 0)) AS fixed_share
          FROM smp p JOIN src s USING (id) LEFT JOIN fx USING (site, id) GROUP BY 1),
  pre AS (SELECT site, APPROX_QUANTILES(n, 2)[OFFSET(1)] AS lead_words FROM lcp GROUP BY 1),
  st AS (SELECT a.site, a.chars, a.fixed_share * a.chars AS static,
                LEAST(a.fixed_share * a.chars, COALESCE(SAFE_DIVIDE(p.lead_words, a.words), 0) * a.chars) AS lead
         FROM agg a LEFT JOIN pre p USING (site))
SELECT st.site, CAST(st.static AS INT64) AS static_chars, CAST(st.chars - st.static AS INT64) AS dynamic_chars,
       CAST(st.static - st.lead AS INT64) AS static_after, ts.tool_sets
FROM st LEFT JOIN (SELECT site, COUNT(DISTINCT tools_hash) AS tool_sets FROM call_rows GROUP BY 1) ts USING (site);

-- 5. the ledger: day-site x model (a day-site is one usecase and day)
CREATE TEMP TABLE prices AS SELECT * FROM UNNEST(@prices);
CREATE TEMP TABLE ledger_daily AS
SELECT c.day, c.usecase, c.site, c.model, COUNT(*) AS calls,
       SUM(c.prompt_tokens) AS prompt_tokens, SUM(c.cached) AS cached_tokens, SUM(c.written) AS written_tokens,
       SUM(c.completion_tokens) AS completion_tokens, COUNTIF(c.cached > 0) AS calls_cached,
       SUM(c.cost) AS cost, SUM(c.cost + c.cached * (p.input - p.cache_read) / 1e6) AS cost_nocache,
       AVG(c.duration_ms) AS avg_ms, AVG(c.n_messages) AS avg_messages, COUNTIF(c.has_tools) AS calls_with_tools
FROM call_rows c LEFT JOIN prices p ON p.model = c.model
GROUP BY 1, 2, 3, 4;

-- 6. gaps: time since the previous call sharing the prefix, bucketed, per day-site and model
CREATE TEMP TABLE cache_gaps AS
WITH seq AS (
  SELECT c.*, TIMESTAMP_DIFF(c.ts, LAG(c.ts) OVER w, SECOND) AS gap,
         MIN(c.prompt_tokens) OVER (PARTITION BY c.site, c.model, c.prefix_key) AS prefix_tokens
  FROM call_rows c WINDOW w AS (PARTITION BY c.site, c.model, c.prefix_key ORDER BY c.ts))
SELECT day, usecase, site, model, CASE WHEN gap IS NULL THEN 'first' {cases} ELSE 'more' END AS bucket,
       COUNT(*) AS calls, SUM(prefix_tokens) AS prefix_tokens, SUM(prompt_tokens) AS prompt_tokens,
       SUM(cached) AS cached, SUM(written) AS written
FROM seq GROUP BY 1, 2, 3, 4, 5;
"""

_DAYSITES = """
SELECT ANY_VALUE(c.usecase) AS usecase, ANY_VALUE(c.day) AS day, CAST(c.site AS STRING) AS site, COUNT(*) AS n,
       ANY_VALUE(t.template) AS template
FROM call_rows c JOIN (SELECT site, ARRAY_AGG(DISTINCT h) AS template FROM templates GROUP BY 1) t USING (site)
GROUP BY c.site
"""
_LEDGER = "SELECT * EXCEPT (site), CAST(site AS STRING) AS site FROM ledger_daily"
_GAPS = "SELECT * EXCEPT (site), CAST(site AS STRING) AS site FROM cache_gaps"
_LAYOUT = "SELECT * EXCEPT (site), CAST(site AS STRING) AS site FROM site_layout"
# request ids per day-site and model for model audits: the extremes (smallest and largest prompt, heaviest answers)
# and POOL more at random; only priced calls that answered
_POOL = """
SELECT CAST(site AS STRING) AS site, model, day, id, prompt_tokens, completion_tokens FROM call_rows
WHERE cost IS NOT NULL AND completion_tokens > 0
QUALIFY ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY FARM_FINGERPRINT(id)) <= {pool}
     OR ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY prompt_tokens, id) = 1
     OR ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY prompt_tokens DESC, id) = 1
     OR ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY completion_tokens DESC, id) <= 2
"""
# the calls no round placed, with their numbers and windows (up to LEFTOVER per usecase and day: the app groups them
# across days), and how many there were
_LEFTOVER = """
SELECT s.id, s.usecase, s.day, s.model, s.prompt_tokens, s.cached AS cached_tokens, s.written AS written_tokens,
       s.completion_tokens, s.cost, s.cost + s.cached * (pr.input - pr.cache_read) / 1e6 AS cost_nocache,
       ARRAY(SELECT DISTINCT h FROM cw WHERE cw.id = s.id) AS template
FROM src s LEFT JOIN placed p USING (id) LEFT JOIN prices pr ON pr.model = s.model
WHERE p.id IS NULL
QUALIFY ROW_NUMBER() OVER (PARTITION BY s.usecase, s.day ORDER BY FARM_FINGERPRINT(s.id)) <= {leftover}
"""
_LEFTOVER_COUNT = "SELECT COUNT(*) AS n FROM src s LEFT JOIN placed p USING (id) WHERE p.id IS NULL"
_SAMPLE = """
SELECT s.sys, s.first_user, s.tool_names FROM src s JOIN call_rows c USING (id) WHERE c.site = @site LIMIT 1
"""
_SAMPLE_ID = "SELECT sys, first_user, tool_names FROM src WHERE id = @id"


_USECASES = """
SELECT `{usecase}` AS usecase, COUNT(*) AS calls, MIN(DATE(`{time}`)) AS first_day, MAX(DATE(`{time}`)) AS last_day
FROM `{table}` WHERE DATE(`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
GROUP BY 1 ORDER BY calls DESC
"""


def _names():
    c = bq.cfg()
    return {**c, "system": _SYSTEM.format(**c),
            "window": ", ' ', ".join(["m"] + ["LEAD(m, %d) OVER w" % j for j in range(1, WINDOW)]),
            "raw_window": ", ' ', ".join(["x"] + ["LEAD(x, %d) OVER w" % j for j in range(1, WINDOW)]),
            "anchor": ANCHOR, "last": WINDOW - 1, "min_calls": MIN_CALLS, "core": CORE, "same": SAME,
            "contain": CONTAIN, "top_k": TOP_K, "sample": SAMPLE, "rounds": ROUNDS, "pairs": PAIRS,
            "cases": " ".join("WHEN gap <= %d THEN '%s'" % (secs, name) for name, secs in BUCKETS)}


def usecases(days=30):
    """Every usecase of the window with its calls (all formats) and days: the usecase and time columns only, cheap."""
    return [dict(r) for r in bq.query(_USECASES.format(**bq.cfg()), {"days": days})]


def _prices():
    from app.catalog import PRICE
    return [{"model": m, "input": float(p["input"]), "cache_read": float(p["cache_read"])} for m, p in PRICE.items()]


def _request(row):
    return {"messages": [{"role": "system", "content": row["sys"]}, {"role": "user", "content": row["first_user"] or ""}],
            "tools": [{"function": {"name": n}} for n in row["tool_names"] or []]}


class Build:
    """Run the day script once for days day_from..day_to (dates, inclusive), then read its results."""
    def __init__(self, day_from, day_to):
        self.s = bq.Session()
        self.s.query(SCRIPT.format(**_names()), {"day_from": day_from, "day_to": day_to, "prices": _prices()})

    def day_sites(self):
        return [dict(r) for r in self.s.query(_DAYSITES)]

    def ledger(self):
        return [dict(r) for r in self.s.query(_LEDGER)]

    def gaps(self):
        return [dict(r) for r in self.s.query(_GAPS)]

    def layout(self):
        return {r["site"]: dict(r) for r in self.s.query(_LAYOUT)}

    def pool(self):
        return [dict(r) for r in self.s.query(_POOL.format(pool=POOL))]

    def leftover(self):
        """(the unplaced calls, capped per usecase and day; how many there were in all)"""
        rows = [dict(r) for r in self.s.query(_LEFTOVER.format(leftover=LEFTOVER))]
        return rows, self.s.query(_LEFTOVER_COUNT)[0]["n"]

    def sample_request(self, site=None, call_id=None):
        """One call (of a day-site, or by id) as a request body: system prompt, first user message, tool names."""
        rows = self.s.query(_SAMPLE, {"site": int(site)}) if site else self.s.query(_SAMPLE_ID, {"id": call_id})
        return _request(rows[0]) if rows else None

    def close(self):
        self.s.close()
