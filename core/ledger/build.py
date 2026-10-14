"""The day script: one BigQuery session, temporary tables only, for a range of days and every usecase at once. Its
results land in the app's database (core/ledger/refresh.py links each day's call-sites to the known ones).

The log is read once (temp table src). BigQuery bills the columns it reads for every day in the range, whatever the
usecase filter (it has no index), so a run covers all usecases: one usecase alone would cost the same.

A call-site's fingerprint is MARKERS phrases of its template: WINDOW-word pieces of its system prompt (lower case,
numbers masked) that nearly all its calls hold and its sibling call-sites don't. Every call is placed by substring
search of the fingerprints of its usecase (@known: what the app already knows), one pass over the day with no row
explosion: the work grows in a straight line with the calls. Only the calls no fingerprint matched are learned from,
in rounds: a sample of up to SAMPLE per usecase and day is cut into windows at anchor words (1 in ANCHOR, chosen by
the word's own hash, so every prompt keeps the same windows); windows in >= MIN_CALLS sampled calls are template
text; calls are keyed by their TOP_K most common windows, re-keyed by windows held by 20..80% of a group (twins),
groups merged when their cores overlap >= SAME; a group of >= MIN_CALLS calls has a template (windows in >= CORE of
its calls) and a fingerprint; the unplaced calls are placed by it, the next round samples what is left (where a rare
call-site is common). Names, dates and retrieved passages of any length don't matter, wherever they sit.

Cache layout per day-site: the prefix as a provider's cache sees it (each call against the previous one of its
call-site, word by word, raw: a changed number breaks it; PAIRS pairs, the median); fixed vs changing text from the
windows of a bounded sample. Scored by tools/identity_score.py on the synthetic log and the hard bench.
"""
from core.ledger import bq

WINDOW = 5
ANCHOR = 8
MIN_CALLS = 5
CORE = 0.9
SAME = 0.8
MARKERS = 5             # phrases per fingerprint
HITS = 3                # phrases a call must hold to be placed (of MARKERS; all of them when there are fewer)
TOP_K = 10
SAMPLE = 5000           # calls per unit and round the groups are learned on
ROUNDS = 3
PAIRS = 30              # consecutive-call pairs per day-site for the prefix
LAYOUT = 200            # calls per day-site the fixed/changing layout is measured on
POOL = 5                # request ids kept per day-site and model for model audits (plus the extremes)
LEFTOVER = 500          # unplaced calls returned per usecase and day
BUCKETS = [("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600), ("6h", 21600), ("24h", 86400)]

_SYSTEM = """COALESCE(JSON_VALUE(`{req}`, '$.messages[0].content'),
           ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(p, '$.text') FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.messages[0].content')) p), '\\n'),
           '')"""

# the masked words of a text, as the windows see them
# (a literal '#' in the text, e.g. a markdown heading, is dropped first so '#' only ever marks a number)
_WORDS = "REGEXP_EXTRACT_ALL(REGEXP_REPLACE(REPLACE(LOWER({text}), '#', ' '), r'[0-9]+', '#'), r'[a-z#]+')"
# the windows at anchor words of the calls in `q` (id, unit, sys), one row each: the phrase (masked words joined by a
# space) and the raw-text hash (for the cache). Row by row with LEAD: fast; q must already be the bounded sample
_WIN = """
SELECT id, unit, CONCAT({phrase}) AS ph, FARM_FINGERPRINT(CONCAT({raw})) AS r
FROM (SELECT id, unit, i, x, REGEXP_REPLACE(x, r'[0-9]+', '#') AS m
      FROM ({q}), UNNEST(REGEXP_EXTRACT_ALL(REPLACE(LOWER(sys), '#', ' '), r'[a-z0-9]+')) x WITH OFFSET i)
QUALIFY MOD(ABS(FARM_FINGERPRINT(m)), {anchor}) = 0 AND LEAD(m, {last}) OVER w IS NOT NULL
WINDOW w AS (PARTITION BY id ORDER BY i)"""
# placement: the fingerprint of the unit's call-sites a call holds >= HITS phrases of (the most), or NULL
_PLACE = """(SELECT AS STRUCT site, hits FROM (
  SELECT k.site, (SELECT COUNTIF(STRPOS(s.norm, CONCAT(' ', ph, ' ')) > 0) FROM UNNEST(k.phrases) ph) AS hits,
         ARRAY_LENGTH(k.phrases) AS n
  FROM UNNEST(fp.sites) k) WHERE hits >= LEAST({hits}, n) ORDER BY hits DESC, site LIMIT 1)"""

SCRIPT = """
DECLARE r INT64 DEFAULT 0;

-- 1. the log, read once: every chat call of the days, its numbers and its system prompt (and the masked text the
--    fingerprints are searched in)
CREATE TEMP TABLE src AS
SELECT *, CONCAT(' ', ARRAY_TO_STRING({words_sys}, ' '), ' ') AS norm FROM (
SELECT CAST(`{id}` AS STRING) AS id, COALESCE(`{usecase}`, '') AS usecase, DATE(`{time}`) AS day, `{time}` AS ts,
       CONCAT(COALESCE(`{usecase}`, ''), '\\x1f', CAST(DATE(`{time}`) AS STRING)) AS unit,
       COALESCE(JSON_VALUE(`{resp}`, '$.model'), JSON_VALUE(`{req}`, '$.model')) AS model,
       FARM_FINGERPRINT(COALESCE(TO_JSON_STRING(ARRAY(SELECT JSON_VALUE(t, '$.function.name')
                                 FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.tools')) t
                                 WHERE JSON_VALUE(t, '$.function.name') IS NOT NULL ORDER BY 1)), '')) AS tools_hash,
       ARRAY(SELECT JSON_VALUE(t, '$.function.name') FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.tools')) t
             WHERE JSON_VALUE(t, '$.function.name') IS NOT NULL) AS tool_names,   -- an array can't hold NULL (built-in tools)
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
WHERE DATE(`{time}`) BETWEEN @day_from AND @day_to AND JSON_QUERY(`{req}`, '$.messages') IS NOT NULL);

-- 2. place every call by the fingerprints the app knows (its usecase's), one pass, row by row
CREATE TEMP TABLE fp AS
SELECT usecase, ARRAY_AGG(STRUCT(site, SPLIT(markers, '|') AS phrases)) AS sites FROM UNNEST(@known) GROUP BY 1;
CREATE TEMP TABLE placed AS
SELECT id, CAST(best.site AS INT64) AS site, -1 AS round FROM (
  SELECT s.id, {place} AS best FROM src s JOIN fp ON fp.usecase = s.usecase)
WHERE best IS NOT NULL;
CREATE TEMP TABLE templates (unit STRING, site INT64, ph STRING, marker BOOL);

-- 3. rounds: learn from a sample of the calls not placed, place by the new fingerprints, repeat on what is left
WHILE r < {rounds} DO
  CREATE OR REPLACE TEMP TABLE cw AS
  SELECT id, unit, ph, FARM_FINGERPRINT(ph) AS h FROM ({win_sample});
  CREATE OR REPLACE TEMP TABLE common AS
  SELECT unit, h, ANY_VALUE(ph) AS ph, COUNT(DISTINCT id) AS n FROM cw GROUP BY 1, 2 HAVING n >= {min_calls};
  -- a call's key: its TOP_K most common windows (the histogram's top), so calls of one template share a key
  CREATE OR REPLACE TEMP TABLE g0 AS
  SELECT id, ANY_VALUE(unit) AS unit,
         FARM_FINGERPRINT(CONCAT(ANY_VALUE(unit), '|', ARRAY_TO_STRING(ARRAY_AGG(CAST(h AS STRING) ORDER BY n DESC, h LIMIT {top_k}), ','))) AS g
  FROM (SELECT DISTINCT w.id, w.unit, w.h, c.n FROM cw w JOIN common c ON c.unit = w.unit AND c.h = w.h)
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
  SELECT g.unit, g.g, w.h, ANY_VALUE(c.ph) AS ph, COUNT(*) AS n FROM g1 g JOIN (SELECT DISTINCT id, unit, h FROM cw) w USING (id)
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
  SELECT c.unit, COALESCE(h2.b, h1.b, c.g) AS g, c.h, ANY_VALUE(c.ph) AS ph, SUM(c.n) AS n
  FROM core c LEFT JOIN hop h1 ON h1.a = c.g LEFT JOIN hop h2 ON h2.a = h1.b GROUP BY 1, 2, 3;
  CREATE OR REPLACE TEMP TABLE tk AS
  SELECT g, ANY_VALUE(unit) AS unit,
         FARM_FINGERPRINT(CONCAT(ANY_VALUE(unit), '|', ARRAY_TO_STRING(ARRAY_AGG(CAST(h AS STRING) ORDER BY CAST(h AS STRING) LIMIT {top_k}), ','))) AS site
  FROM newt GROUP BY 1;
  -- the fingerprint: MARKERS phrases of the template no other call-site of the unit shares (this round's, or known)
  INSERT INTO templates
  SELECT t.unit, k.site, t.ph,
         ROW_NUMBER() OVER (PARTITION BY t.g ORDER BY o.h IS NULL DESC, kn.ph IS NULL DESC, t.n DESC, t.ph) <= {markers} AS marker
  FROM newt t JOIN tk k USING (g)
  LEFT JOIN (SELECT unit, h FROM newt GROUP BY 1, 2 HAVING COUNT(DISTINCT g) > 1) o ON o.unit = t.unit AND o.h = t.h
  LEFT JOIN (SELECT usecase, ph FROM UNNEST(@known), UNNEST(SPLIT(template, '|')) ph) kn
         ON kn.usecase = SPLIT(t.unit, '\\x1f')[OFFSET(0)] AND kn.ph = t.ph;
  CREATE OR REPLACE TEMP TABLE fp AS
  SELECT unit, ARRAY_AGG(STRUCT(CAST(site AS STRING) AS site, phrases)) AS sites FROM (
    SELECT unit, site, ARRAY_AGG(ph) AS phrases FROM templates WHERE marker AND site IN (SELECT site FROM tk) GROUP BY 1, 2)
  GROUP BY 1;
  INSERT INTO placed
  SELECT id, CAST(best.site AS INT64), r FROM (
    SELECT s.id, {place} AS best FROM src s LEFT JOIN placed p USING (id) JOIN fp ON fp.unit = s.unit WHERE p.id IS NULL)
  WHERE best IS NOT NULL;
  SET r = r + 1;
END WHILE;

CREATE TEMP TABLE call_rows AS SELECT s.* EXCEPT (sys, first_user, tool_names, norm), p.site FROM src s JOIN placed p USING (id);

-- 4. per day-site: the prompt's layout for the cache (bounded samples per site)
CREATE TEMP TABLE pairs AS
SELECT site, id, prev FROM (
  SELECT c.site, c.id, LAG(c.id) OVER (PARTITION BY c.site ORDER BY c.ts, c.id) AS prev FROM call_rows c)
WHERE prev IS NOT NULL QUALIFY ROW_NUMBER() OVER (PARTITION BY site ORDER BY FARM_FINGERPRINT(id)) <= {pairs};
CREATE TEMP TABLE lcp AS
WITH pw AS (SELECT s.id, i, x FROM src s, UNNEST(REGEXP_EXTRACT_ALL(LOWER(s.sys), r'[a-z0-9]+')) x WITH OFFSET i
            WHERE s.id IN (SELECT id FROM pairs UNION ALL SELECT prev FROM pairs))
SELECT p.site, p.id, COALESCE(MIN(IF(a.x != b.x, a.i, NULL)), COUNT(*)) AS n
FROM pairs p JOIN pw a ON a.id = p.id JOIN pw b ON b.id = p.prev AND b.i = a.i GROUP BY 1, 2;
CREATE TEMP TABLE lay_smp AS
SELECT c.site, c.id, s.sys FROM call_rows c JOIN src s USING (id)
QUALIFY ROW_NUMBER() OVER (PARTITION BY c.site ORDER BY FARM_FINGERPRINT(c.id)) <= {layout};
CREATE TEMP TABLE lay_w AS SELECT DISTINCT CAST(unit AS INT64) AS site, id, r FROM ({win_layout});
CREATE TEMP TABLE lay_fixed AS
SELECT w.site, w.r FROM lay_w w JOIN (SELECT site, COUNT(*) AS n FROM lay_smp GROUP BY 1) k USING (site)
GROUP BY 1, 2 HAVING COUNT(*) >= 0.5 * ANY_VALUE(k.n);
CREATE TEMP TABLE lay_fx AS
SELECT w.site, w.id, COUNTIF(f.r IS NOT NULL) / COUNT(*) AS fixed_share
FROM lay_w w LEFT JOIN lay_fixed f ON f.site = w.site AND f.r = w.r GROUP BY 1, 2;
CREATE TEMP TABLE site_layout AS
WITH agg AS (SELECT p.site, AVG(LENGTH(p.sys)) AS chars, AVG(ARRAY_LENGTH(REGEXP_EXTRACT_ALL(LOWER(p.sys), r'[a-z0-9]+'))) AS words,
                    AVG(COALESCE(fx.fixed_share, 0)) AS fixed_share
             FROM lay_smp p LEFT JOIN lay_fx fx USING (site, id) GROUP BY 1),
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

-- 7. what the app reads: the day's call-sites with their templates and fingerprints, one sample call per call-site,
--    request ids for audits (the extremes and POOL at random per call-site and model), the unplaced calls (capped)
CREATE TEMP TABLE day_sites AS
SELECT ANY_VALUE(c.usecase) AS usecase, ANY_VALUE(c.day) AS day, CAST(c.site AS STRING) AS site, COUNT(*) AS n,
       ANY_VALUE(t.template) AS template, ANY_VALUE(t.markers) AS markers
FROM call_rows c LEFT JOIN (SELECT site, ARRAY_AGG(DISTINCT ph) AS template, ARRAY_AGG(IF(marker, ph, NULL) IGNORE NULLS) AS markers
                            FROM templates GROUP BY 1) t USING (site)
GROUP BY c.site;
CREATE TEMP TABLE site_sample AS
SELECT CAST(c.site AS STRING) AS site, s.sys, s.first_user, s.tool_names FROM call_rows c JOIN src s USING (id)
QUALIFY ROW_NUMBER() OVER (PARTITION BY c.site ORDER BY FARM_FINGERPRINT(c.id)) = 1;
CREATE TEMP TABLE pool AS
SELECT CAST(site AS STRING) AS site, model, day, id, prompt_tokens, completion_tokens, duration_ms FROM call_rows
WHERE cost IS NOT NULL AND completion_tokens > 0
QUALIFY ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY FARM_FINGERPRINT(id)) <= {pool}
     OR ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY prompt_tokens, id) = 1
     OR ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY prompt_tokens DESC, id) = 1
     OR ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY completion_tokens DESC, id) <= 2
     OR ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY duration_ms DESC, id) <= 2;
CREATE TEMP TABLE leftover_count AS SELECT COUNT(*) AS n FROM src s LEFT JOIN placed p USING (id) WHERE p.id IS NULL;
CREATE TEMP TABLE leftover AS
SELECT s.id, s.usecase, s.day, s.model, s.prompt_tokens, s.cached AS cached_tokens, s.written AS written_tokens,
       s.completion_tokens, s.cost, s.cost + s.cached * (pr.input - pr.cache_read) / 1e6 AS cost_nocache,
       ARRAY(SELECT DISTINCT ph FROM UNNEST(w.phs) ph) AS template
FROM (SELECT * FROM src s LEFT JOIN placed p USING (id) WHERE p.id IS NULL
      QUALIFY ROW_NUMBER() OVER (PARTITION BY s.usecase, s.day ORDER BY FARM_FINGERPRINT(s.id)) <= {leftover}) s
LEFT JOIN prices pr ON pr.model = s.model
LEFT JOIN (SELECT id, ARRAY_AGG(ph) AS phs FROM ({win_leftover}) GROUP BY 1) w USING (id);
"""

_DAYSITES = "SELECT * FROM day_sites"
_LEDGER = "SELECT * EXCEPT (site), CAST(site AS STRING) AS site FROM ledger_daily"
_GAPS = "SELECT * EXCEPT (site), CAST(site AS STRING) AS site FROM cache_gaps"
_LAYOUT = "SELECT * EXCEPT (site), CAST(site AS STRING) AS site FROM site_layout"
_POOL = "SELECT * FROM pool"
_LEFTOVER = "SELECT * FROM leftover"
_LEFTOVER_COUNT = "SELECT n FROM leftover_count"
_SAMPLE = "SELECT sys, first_user, tool_names FROM site_sample WHERE site = @site"
_SAMPLE_ID = "SELECT sys, first_user, tool_names FROM src WHERE id = @id"


_USECASES = """
SELECT `{usecase}` AS usecase, COUNT(*) AS calls, MIN(DATE(`{time}`)) AS first_day, MAX(DATE(`{time}`)) AS last_day
FROM `{table}` WHERE DATE(`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
GROUP BY 1 ORDER BY calls DESC
"""


def _names():
    c = bq.cfg()
    words = _WORDS.format(text="sys")
    win = {"anchor": ANCHOR, "last": WINDOW - 1,
           "phrase": ", ' ', ".join(["m"] + ["LEAD(m, %d) OVER w" % j for j in range(1, WINDOW)]),
           "raw": ", ' ', ".join(["x"] + ["LEAD(x, %d) OVER w" % j for j in range(1, WINDOW)])}
    sample = ("SELECT s.id, s.unit, s.sys FROM src s LEFT JOIN placed p USING (id) WHERE p.id IS NULL "
              "QUALIFY ROW_NUMBER() OVER (PARTITION BY s.unit ORDER BY FARM_FINGERPRINT(CONCAT(s.id, CAST(r AS STRING)))) <= %d" % SAMPLE)
    layout = "SELECT id, CAST(site AS STRING) AS unit, sys FROM lay_smp"
    leftover = ("SELECT s.id, s.unit, s.sys FROM src s LEFT JOIN placed p USING (id) WHERE p.id IS NULL "
                "QUALIFY ROW_NUMBER() OVER (PARTITION BY s.usecase, s.day ORDER BY FARM_FINGERPRINT(s.id)) <= %d" % LEFTOVER)
    return {**c, "system": _SYSTEM.format(**c), "words_sys": words,
            "win_sample": _WIN.format(q=sample, **win), "win_layout": _WIN.format(q=layout, **win),
            "win_leftover": _WIN.format(q=leftover, **win),
            "place": _PLACE.format(hits=HITS), "min_calls": MIN_CALLS, "core": CORE, "same": SAME, "markers": MARKERS,
            "top_k": TOP_K, "sample": SAMPLE, "rounds": ROUNDS, "pairs": PAIRS, "layout": LAYOUT, "pool": POOL, "leftover": LEFTOVER,
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


_STEPS = (("src", "reading the day's calls"), ("placed", "placing calls by fingerprint"), ("templates", "learning call-sites"),
          ("cw", "learning call-sites"), ("common", "learning call-sites"), ("g0", "learning call-sites"), ("g1", "learning call-sites"),
          ("splitter", "learning call-sites"), ("core", "learning call-sites"), ("hop", "learning call-sites"), ("newt", "learning call-sites"),
          ("tk", "learning call-sites"), ("fp", "learning call-sites"), ("call_rows", "placing calls by fingerprint"),
          ("pairs", "cache prefix"), ("lcp", "cache prefix"), ("lay_", "prompt layout"), ("site_layout", "prompt layout"),
          ("prices", "totals"), ("ledger_daily", "totals"), ("cache_gaps", "cache gaps"), ("day_sites", "collecting the results"),
          ("site_sample", "collecting the results"), ("pool", "audit samples"), ("leftover", "unplaced calls"))


def _plain(text):
    """A script statement's head ("CREATE OR REPLACE TEMP TABLE g0 AS (12 statements done, 1 min)") in plain words."""
    name = text.replace("CREATE OR REPLACE TEMP TABLE ", "").replace("CREATE TEMP TABLE ", "").replace("INSERT INTO ", "").split(" ")[0]
    tail = text[text.rfind("("):] if "(" in text else ""
    return "%s %s" % (next((w for k, w in _STEPS if name.startswith(k)), name), tail)


class Build:
    """Run the day script once for days day_from..day_to (dates, inclusive) with the fingerprints known
    (known: [{usecase, site, markers, template}], phrases joined by '|'), then read its results."""
    def __init__(self, day_from, day_to, known=(), session_id=None):
        """With `session_id`, a session whose script already finished (its tables are reused: no read of the day)."""
        self.s = bq.Session(session_id) if session_id else bq.Session()
        self.resumed = bool(session_id) and self.s.has("leftover")
        if self.resumed:
            return
        if session_id:                                      # the session is gone or incomplete: start afresh
            self.s = bq.Session()
        known = [{"usecase": k["usecase"] or "", "site": str(k["site"]), "markers": k["markers"], "template": k["template"]}
                 for k in known] or [{"usecase": "\x00", "site": "0", "markers": "", "template": ""}]   # a typed, empty array
        from core import tasks
        self.s.script(SCRIPT.format(**_names()), {"day_from": day_from, "day_to": day_to, "prices": _prices(), "known": known},
                      progress=lambda text: tasks.note("%s: %s" % (day_from, _plain(text))))

    def _read(self, sql, params=None):
        return [dict(r) for r in self.s.query(sql, params, timeout=4 * 3600)]

    def day_sites(self):
        return self._read(_DAYSITES)

    def ledger(self):
        return self._read(_LEDGER)

    def gaps(self):
        return self._read(_GAPS)

    def layout(self):
        return {r["site"]: r for r in self._read(_LAYOUT)}

    def pool(self):
        return self._read(_POOL)

    def leftover(self):
        """(the unplaced calls, capped per usecase and day; how many there were in all)"""
        return self._read(_LEFTOVER), self._read(_LEFTOVER_COUNT)[0]["n"]

    def sample_request(self, site=None, call_id=None):
        """One call (of a day-site, or by id) as a request body: system prompt, first user message, tool names."""
        rows = self._read(_SAMPLE, {"site": str(site)}) if site else self._read(_SAMPLE_ID, {"id": call_id})
        return _request(rows[0]) if rows else None

    def close(self):
        self.s.close()
