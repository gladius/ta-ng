"""The day script: one BigQuery session, temporary tables only, for a range of days and every usecase at once. Its
results land in the app's database (core/ledger/refresh.py).

The log is read once (temp table src, clustered by usecase: every later statement that names a usecase reads its
slice only). BigQuery bills the columns it reads for every day in the range, whatever the usecase filter, so a run
covers all usecases: one usecase alone would cost the same.

A call-site is one place in the code that sends a prompt: its tool set and its system prompt template. Its
fingerprint is a few literal phrases of that template (lower case, numbers masked), chosen by a model from three of
its calls and proved by count (core/identity/loop.py); a call is placed when its head holds `need` of them. The
script places every call by the fingerprints the app knows (@known) in one pass; the loop then works the rest, seed
by seed, with the statements below (SEEDS, HITS, PLACE); FINISH computes the ledger, the cache gaps and the prompt
layout per day-site and collects what the app downloads.

Cache layout per day-site: the prefix as a provider's cache sees it (each call against the previous one of its
call-site, word by word, raw: a changed number breaks it; PAIRS pairs, the median). Scored by tools/identity_score.py
on the synthetic log and the hard bench."""
import credentials
from core.ledger import bq

MIN_CALLS = 5            # a call-site has at least this many calls a day; a seed's phrases must place this many
PREFIX = 300             # masked characters of the head that key the free pass (seeds are its biggest keys)
SEEDS = 5                # seeds per usecase and round
HEADS = 3                # calls of a seed shown to the model
PAIRS = 30               # consecutive-call pairs per day-site for the prefix
POOL = 5                 # request ids kept per day-site and model for model audits (plus the extremes)
LEFTOVER = 500           # unplaced calls returned per usecase and day
BUCKETS = [("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600), ("6h", 21600), ("24h", 86400)]
HEAD_CAP = 16000         # characters kept of a first message that is not a system prompt, and of the tool schema text
MAP_SAMPLE = 100         # calls per day-site the prompt map's shares are counted over

# a message's text: a string content, or the text parts of a list content
_TEXT = """COALESCE(JSON_VALUE(m, '$.content'),
                ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(p, '$.text') FROM UNNEST(JSON_QUERY_ARRAY(m, '$.content')) p), '\\n'), '')"""
# the system text of a chat call (the head of the request, after the tool names): the first message's text when it is
# the system prompt (most calls: no walk over the messages); otherwise every system / developer message; with none
# (a team putting its instructions in a "user" message, or an empty first message) the first message that has text,
# capped at HEAD_CAP characters
_SYSTEM = """IF(JSON_VALUE(`{req}`, '$.messages[0].role') IN ('system', 'developer'),
  (SELECT {text} FROM UNNEST([JSON_QUERY(`{req}`, '$.messages[0]')]) m),
  COALESCE(NULLIF(ARRAY_TO_STRING(ARRAY(SELECT {text} FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.messages')) m WITH OFFSET o
                                        WHERE JSON_VALUE(m, '$.role') IN ('system', 'developer') ORDER BY o), '\\n'), ''),
           (SELECT SUBSTR(x, 1, {head_cap}) FROM (SELECT {text} AS x, o FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.messages')) m WITH OFFSET o)
            WHERE LENGTH(TRIM(x)) > 0 ORDER BY o LIMIT 1),
           ''))"""
# the masked words of a text, as phrases are matched (core.identity.loop.mask does the same in Python):
# a literal '#' is dropped first so '#' only ever marks a number
_WORDS = "REGEXP_EXTRACT_ALL(REGEXP_REPLACE(REPLACE(LOWER({text}), '#', ' '), r'[0-9]+', '#'), r'[a-z#]+')"
# placement: the fingerprint of the usecase's call-sites a call holds `need` phrases of (the most), or NULL
_PLACE = """(SELECT AS STRUCT site, hits FROM (
  SELECT k.site, (SELECT COUNTIF(STRPOS(s.norm, CONCAT(' ', ph, ' ')) > 0) FROM UNNEST(k.phrases) ph) AS hits, k.need
  FROM UNNEST(fp.sites) k) WHERE hits >= need ORDER BY hits DESC, need, site LIMIT 1)"""

READ = """
CREATE TEMP TABLE prices AS SELECT * FROM UNNEST(@prices);

-- 1. the log, read once: every row of the days (all formats), its numbers, and for chat calls the head of the request
--    (tool names, then the system prompt) and its masked text, which fingerprints are searched in
CREATE TEMP TABLE src CLUSTER BY usecase AS
SELECT m.*, CONCAT(m.log_id, '@', CAST(m.ts_us AS STRING)) AS id, FARM_FINGERPRINT(m.sys) AS prefix_key,
       FARM_FINGERPRINT(CONCAT(CAST(m.tools_hash AS STRING), '|', SUBSTR(m.norm, 1, {prefix}))) AS key, FARM_FINGERPRINT(m.norm) AS whole,
       m.cost + m.cached * (m.price_input - m.price_cache_read) / 1e6 AS cost_nocache
FROM (
  SELECT n.*, CONCAT(' ', ARRAY_TO_STRING({words_sys}, ' '), ' ') AS norm FROM (
    SELECT s.* EXCEPT (cost_logged, model_key),
           CONCAT(IF(ARRAY_LENGTH(tool_names) > 0, CONCAT('tools: ', ARRAY_TO_STRING(ARRAY(SELECT x FROM UNNEST(tool_names) x ORDER BY x), ' '), '\\n'), ''),
                  sys_text) AS sys,
           -- the logged cost, else tokens x the catalog price; unknown (NULL) when the call has no usage or no price
           COALESCE(cost_logged, IF(has_usage, ((prompt_tokens - cached - written) * pr.input + cached * pr.cache_read
                                                 + written * COALESCE(NULLIF(pr.cache_write, 0), pr.input) + completion_tokens * pr.output) / 1e6, NULL)) AS cost,
           pr.input AS price_input, pr.cache_read AS price_cache_read,
           FARM_FINGERPRINT(ARRAY_TO_STRING(ARRAY(SELECT x FROM UNNEST(tool_names) x ORDER BY x), ',')) AS tools_hash
    FROM (
      SELECT COALESCE(CAST(`{id}` AS STRING), '') AS log_id, UNIX_MICROS(CAST(`{time}` AS TIMESTAMP)) AS ts_us,
             COALESCE(`{usecase}`, '') AS usecase, DATE(`{time}`) AS day, `{time}` AS ts,
             JSON_QUERY(`{req}`, '$.messages') IS NOT NULL AS chat,
             JSON_VALUE(`{resp}`, '$.usage.prompt_tokens') IS NOT NULL AS has_usage,
             COALESCE({served}, JSON_VALUE(`{resp}`, '$.model'), JSON_VALUE(`{req}`, '$.model')) AS model,
             REGEXP_REPLACE(REGEXP_REPLACE(LOWER(COALESCE({served}, JSON_VALUE(`{resp}`, '$.model'), JSON_VALUE(`{req}`, '$.model'))),
                                           r'^.*/', ''), r'[-_@:](\\d{{4}}-?\\d{{2}}-?\\d{{2}}|\\d{{6,8}}|v\\d+|latest|preview|exp)$', '') AS model_key,
             ARRAY(SELECT JSON_VALUE(t, '$.function.name') FROM UNNEST(JSON_QUERY_ARRAY(`{req}`, '$.tools')) t
                   WHERE JSON_VALUE(t, '$.function.name') IS NOT NULL) AS tool_names,   -- an array can't hold NULL (built-in tools)
             CAST(JSON_VALUE(`{resp}`, '$.usage.prompt_tokens') AS INT64) AS prompt_tokens,
             GREATEST(COALESCE(CAST(JSON_VALUE(`{resp}`, '$.usage.cache_read_input_tokens') AS INT64), 0),
                      COALESCE(CAST(JSON_VALUE(`{resp}`, '$.usage.prompt_tokens_details.cached_tokens') AS INT64), 0)) AS cached,
             COALESCE(CAST(JSON_VALUE(`{resp}`, '$.usage.cache_creation_input_tokens') AS INT64), 0) AS written,
             CAST(JSON_VALUE(`{resp}`, '$.usage.completion_tokens') AS INT64) AS completion_tokens,
             CAST(JSON_VALUE(`{resp}`, '$.usage.cost') AS FLOAT64) AS cost_logged,
             CAST(`{latency}` AS FLOAT64) AS duration_ms,
             ARRAY_LENGTH(JSON_QUERY_ARRAY(`{req}`, '$.messages')) AS n_messages,
             JSON_QUERY(`{req}`, '$.tools') IS NOT NULL AS has_tools,
             {system} AS sys_text,
             SUBSTR(JSON_VALUE(`{req}`, '$.messages[1].content'), 1, 2000) AS first_user,
             SUBSTR(TO_JSON_STRING(JSON_QUERY(`{req}`, '$.tools')), 1, {head_cap}) AS tools_text,      -- the schemas, for the prompt map
             FARM_FINGERPRINT(COALESCE(TO_JSON_STRING(JSON_QUERY(`{req}`, '$.tools')), '')) AS schema_hash   -- a changed description breaks the cache too
      FROM `{table}`
      WHERE DATE(`{time}`) BETWEEN @day_from AND @day_to AND {skip}) s
    LEFT JOIN prices pr ON pr.model = s.model_key) n) m;

-- the day's make-up per usecase, before anything is dropped (shown as coverage)
CREATE TEMP TABLE cov0 AS
SELECT usecase, day, COUNT(*) AS calls, COUNTIF(NOT chat) AS other_format, COUNTIF(chat AND NOT has_usage) AS no_usage,
       COUNTIF(chat AND has_usage AND cost IS NULL) AS unpriced,
       COUNTIF(chat) - COUNT(DISTINCT IF(chat, id, NULL)) AS repeated
FROM src GROUP BY 1, 2;

-- chat calls, each once, their numbers only (a row logged twice, same id and microsecond, is one call)
CREATE TEMP TABLE calls CLUSTER BY usecase AS
SELECT * EXCEPT (sys, sys_text, norm, first_user, tool_names, tools_text, chat) FROM src WHERE chat
QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY prompt_tokens DESC) = 1;

-- 2. place every call by the fingerprints the app knows (its usecase's), one pass, row by row
CREATE TEMP TABLE fp AS
SELECT usecase, ARRAY_AGG(STRUCT(site, SPLIT(markers, '|') AS phrases, need)) AS sites FROM UNNEST(@known) GROUP BY 1;
CREATE TEMP TABLE placed (id STRING, site STRING, round INT64);
INSERT INTO placed
SELECT DISTINCT id, best.site, 0 FROM (
  SELECT s.id, {place} AS best FROM src s JOIN fp ON fp.usecase = s.usecase WHERE s.chat)
WHERE best IS NOT NULL;
"""

# the day read already (its session alive): the placement redone with the fingerprints known now, the loop then runs again
REPLACE = """
CREATE OR REPLACE TEMP TABLE fp AS
SELECT usecase, ARRAY_AGG(STRUCT(site, SPLIT(markers, '|') AS phrases, need)) AS sites FROM UNNEST(@known) GROUP BY 1;
CREATE OR REPLACE TEMP TABLE placed (id STRING, site STRING, round INT64);
INSERT INTO placed
SELECT DISTINCT id, best.site, 0 FROM (
  SELECT s.id, {place} AS best FROM src s JOIN fp ON fp.usecase = s.usecase WHERE s.chat)
WHERE best IS NOT NULL;
"""

# the loop's statements (core/identity/loop.py). SEEDS: for the live usecases (@live), the unplaced calls left (n_left),
# their SEEDS biggest keys (tool set + first PREFIX masked characters) with HEADS calls each, plus HEADS calls at random
# (key NULL) for when no key is big enough (the template's fixed text starts after a changing one, e.g. a date first).
# Calls already shown to the model (@asked) come last in a key and never in the random pool: a seed is not re-asked.
_SEEDS = """
WITH rest AS (
  SELECT s.id, s.usecase, s.key, s.whole, s.sys, s.tool_names, s.model, COUNT(*) OVER (PARTITION BY s.usecase) AS n_left
  FROM src s LEFT JOIN placed p USING (id) WHERE s.chat AND p.id IS NULL AND s.usecase IN UNNEST(@live)),
keys AS (
  SELECT usecase, key, COUNT(*) AS n, COUNT(DISTINCT whole) = 1 AS static FROM rest GROUP BY 1, 2
  QUALIFY ROW_NUMBER() OVER (PARTITION BY usecase ORDER BY n DESC, key) <= {seeds})
SELECT l.usecase, l.key, k.n, k.static, l.n_left, l.id, l.sys, l.tool_names, l.model
FROM rest l JOIN keys k USING (usecase, key)
QUALIFY ROW_NUMBER() OVER (PARTITION BY l.usecase, l.key ORDER BY l.id IN UNNEST(@asked), FARM_FINGERPRINT(l.id)) <= {heads}
UNION ALL
SELECT usecase, CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS BOOL), n_left, id, sys, tool_names, model FROM rest
WHERE id NOT IN UNNEST(@asked)
QUALIFY ROW_NUMBER() OVER (PARTITION BY usecase ORDER BY FARM_FINGERPRINT(CONCAT(id, CAST(@round AS STRING)))) <= {heads}
"""
# HITS: for this round's candidates (@cands: usecase, cand, phrases joined by '|', need), every call of the usecase holding `need`
# of the phrases, with the site it is already placed in (NULL: free)
_HITS = """
CREATE OR REPLACE TEMP TABLE cand AS SELECT * FROM UNNEST(@cands);
CREATE OR REPLACE TEMP TABLE hits AS
SELECT * FROM (
  SELECT s.id, s.usecase, c.cand, c.need, p.site AS placed_site,
         (SELECT COUNTIF(STRPOS(s.norm, CONCAT(' ', ph, ' ')) > 0) FROM UNNEST(SPLIT(c.phrases, '|')) ph) AS hits
  FROM src s JOIN cand c ON c.usecase = s.usecase LEFT JOIN placed p ON p.id = s.id WHERE s.chat)
WHERE hits >= need;
"""
_HIT_COUNTS = """
SELECT cand, COUNTIF(placed_site IS NULL) AS free, COUNT(DISTINCT IF(placed_site IS NULL, id, NULL)) AS free_ids,
       ARRAY_AGG(STRUCT(placed_site AS site, n) ORDER BY n DESC LIMIT 3) AS collisions
FROM (SELECT cand, id, placed_site, COUNT(*) OVER (PARTITION BY cand, placed_site) AS n FROM hits)
GROUP BY cand
"""
_OVERLAPS = """
SELECT a.cand AS a, b.cand AS b, COUNT(DISTINCT a.id) AS n FROM hits a JOIN hits b ON a.id = b.id AND a.cand < b.cand
WHERE a.placed_site IS NULL GROUP BY 1, 2
"""
# PLACE: the free calls matched by the accepted candidates (@accepted: cand, site), each call to the one it holds the
# most phrases of
_PLACE_ROUND = """
INSERT INTO placed
SELECT id, site, @round FROM (
  SELECT h.id, a.site, ROW_NUMBER() OVER (PARTITION BY h.id ORDER BY h.hits DESC, a.site) AS rn
  FROM hits h JOIN UNNEST(@accepted) a ON a.cand = h.cand WHERE h.placed_site IS NULL)
WHERE rn = 1
"""
_SITE_COUNTS = "SELECT site, COUNT(DISTINCT id) AS n FROM placed WHERE site IN UNNEST(@sites) GROUP BY 1"
_MOVE = "UPDATE placed SET site = @to WHERE site = @frm"
_SITE_HEADS = """
SELECT p.site, s.sys, s.model FROM src s JOIN placed p ON p.id = s.id
JOIN UNNEST(@sites) k ON k.usecase = s.usecase AND k.site = p.site AND k.rnd = p.round
WHERE s.chat AND s.id NOT IN UNNEST(@not)
QUALIFY ROW_NUMBER() OVER (PARTITION BY p.site ORDER BY FARM_FINGERPRINT(CONCAT(s.id, 'h'))) <= {heads}"""

FINISH = """
CREATE OR REPLACE TEMP TABLE call_rows AS SELECT c.*, p.site FROM calls c JOIN placed p USING (id);

-- 4. per day-site: the prompt's prefix for the cache (PAIRS consecutive-call pairs, word by word, raw)
CREATE OR REPLACE TEMP TABLE pairs AS
SELECT site, id, prev FROM (
  SELECT c.site, c.id, LAG(c.id) OVER (PARTITION BY c.site ORDER BY c.ts, c.id) AS prev FROM call_rows c)
WHERE prev IS NOT NULL QUALIFY ROW_NUMBER() OVER (PARTITION BY site ORDER BY FARM_FINGERPRINT(id)) <= {pairs};
CREATE OR REPLACE TEMP TABLE lcp AS
WITH pw AS (SELECT s.id, i, x FROM src s, UNNEST(REGEXP_EXTRACT_ALL(LOWER(s.sys), r'[a-z0-9]+')) x WITH OFFSET i
            WHERE s.chat AND s.id IN (SELECT id FROM pairs UNION ALL SELECT prev FROM pairs))
SELECT p.site, p.id, COALESCE(MIN(IF(a.x != b.x, a.i, NULL)), COUNT(*)) AS n
FROM pairs p JOIN pw a ON a.id = p.id JOIN pw b ON b.id = p.prev AND b.i = a.i GROUP BY 1, 2;
CREATE OR REPLACE TEMP TABLE site_layout AS
WITH agg AS (SELECT c.site, AVG(LENGTH(s.sys)) AS chars, AVG(ARRAY_LENGTH(REGEXP_EXTRACT_ALL(LOWER(s.sys), r'[a-z0-9]+'))) AS words
             FROM call_rows c JOIN src s USING (id) WHERE s.chat AND c.id IN (SELECT id FROM pairs) GROUP BY 1),
  pre AS (SELECT site, APPROX_QUANTILES(n, 2)[OFFSET(1)] AS lead_words FROM lcp GROUP BY 1)
SELECT a.site, CAST(a.chars AS INT64) AS chars, CAST(COALESCE(SAFE_DIVIDE(p.lead_words, a.words), 0) * a.chars AS INT64) AS lead_chars, ts.tool_sets
FROM agg a LEFT JOIN pre p USING (site)
LEFT JOIN (SELECT site, COUNT(DISTINCT schema_hash) AS tool_sets FROM call_rows GROUP BY 1) ts USING (site);

-- 5. the ledger: day-site x model (a day-site is one usecase and day)
CREATE OR REPLACE TEMP TABLE ledger_daily AS
SELECT c.day, c.usecase, c.site, c.model, COUNT(*) AS calls,
       SUM(c.prompt_tokens) AS prompt_tokens, SUM(c.cached) AS cached_tokens, SUM(c.written) AS written_tokens,
       SUM(c.completion_tokens) AS completion_tokens, COUNTIF(c.cached > 0) AS calls_cached,
       SUM(c.cost) AS cost, SUM(c.cost_nocache) AS cost_nocache, COUNTIF(c.cost IS NULL) AS calls_unpriced,
       AVG(c.duration_ms) AS avg_ms, AVG(c.n_messages) AS avg_messages, COUNTIF(c.has_tools) AS calls_with_tools,
       SUM(IF(c.cached > 0, c.duration_ms, 0)) AS ms_hit, SUM(IF(c.cached > 0, 0, c.duration_ms)) AS ms_miss,
       COUNTIF(c.cached > 0 AND c.duration_ms IS NOT NULL) AS timed_hit, COUNTIF(c.cached = 0 AND c.duration_ms IS NOT NULL) AS timed_miss
FROM call_rows c
GROUP BY 1, 2, 3, 4;

-- 6. gaps: time since the previous call sharing the prefix, bucketed, per day-site and model
CREATE OR REPLACE TEMP TABLE cache_gaps AS
WITH seq AS (
  SELECT c.*, TIMESTAMP_DIFF(c.ts, LAG(c.ts) OVER w, SECOND) AS gap,
         MIN(c.prompt_tokens) OVER (PARTITION BY c.site, c.model, c.prefix_key) AS prefix_tokens
  FROM call_rows c WINDOW w AS (PARTITION BY c.site, c.model, c.prefix_key ORDER BY c.ts))
SELECT day, usecase, site, model, CASE WHEN gap IS NULL THEN 'first' {cases} ELSE 'more' END AS bucket,
       COUNT(*) AS calls, SUM(prefix_tokens) AS prefix_tokens, SUM(prompt_tokens) AS prompt_tokens,
       SUM(cached) AS cached, SUM(written) AS written
FROM seq GROUP BY 1, 2, 3, 4, 5;

-- 7. what the app reads: the day's call-sites, a few calls each (text, one read), request ids for audits (the
--    extremes and POOL at random per call-site and model), the unplaced calls (capped), the coverage
CREATE OR REPLACE TEMP TABLE day_sites AS
SELECT ANY_VALUE(usecase) AS usecase, ANY_VALUE(day) AS day, site, COUNT(*) AS n FROM call_rows GROUP BY site;
CREATE OR REPLACE TEMP TABLE site_calls AS
SELECT c.site, c.usecase, c.id, c.model, s.sys, s.first_user, s.tool_names,
       ROW_NUMBER() OVER (PARTITION BY c.site ORDER BY FARM_FINGERPRINT(c.id)) AS rn
FROM call_rows c JOIN src s USING (id) WHERE s.chat
QUALIFY ROW_NUMBER() OVER (PARTITION BY c.site ORDER BY FARM_FINGERPRINT(c.id)) <= 3
     OR ROW_NUMBER() OVER (PARTITION BY c.site, c.model ORDER BY FARM_FINGERPRINT(c.id)) = 1;
-- 7b. the prompt map, in the provider's cache order (tools, system, first message): per segment of the sample head
--     (site_calls rn = 1), its word 4-grams in order, each with the share of MAP_SAMPLE calls holding it (one digit
--     per gram, 0-9), its masked words, its length and its raw text: the static / per-call squares the pages draw
CREATE OR REPLACE TEMP TABLE site_grams AS
WITH head AS (SELECT sc.site, s.tools_text, s.sys_text, s.first_user FROM site_calls sc JOIN src s USING (id) WHERE sc.rn = 1),
seg AS (
  SELECT site, 'tools' AS kind, 0 AS ord, {words_tools} AS w, LENGTH(tools_text) AS chars, tools_text AS sample FROM head
  UNION ALL SELECT site, 'system', 1, {words_text}, LENGTH(sys_text), sys_text FROM head
  UNION ALL SELECT site, 'user', 2, {words_user}, LENGTH(first_user), first_user FROM head),
grams AS (SELECT s.site, s.kind, i, g FROM seg s, UNNEST(ML.NGRAMS(s.w, [4, 4])) g WITH OFFSET i),
sample AS (SELECT c.site, c.id, s.tools_text, s.sys_text, s.first_user FROM call_rows c JOIN src s USING (id) WHERE s.chat
           QUALIFY ROW_NUMBER() OVER (PARTITION BY c.site ORDER BY FARM_FINGERPRINT(CONCAT(c.id, 'map'))) <= {map_sample}),
n AS (SELECT site, COUNT(*) AS n FROM sample GROUP BY 1),
sgram AS (
  SELECT site, 'tools' AS kind, id, g FROM sample, UNNEST(ML.NGRAMS({words_tools}, [4, 4])) g
  UNION ALL SELECT site, 'system', id, g FROM sample, UNNEST(ML.NGRAMS({words_text}, [4, 4])) g
  UNION ALL SELECT site, 'user', id, g FROM sample, UNNEST(ML.NGRAMS({words_user}, [4, 4])) g),
cnt AS (SELECT site, kind, g, COUNT(DISTINCT id) AS k FROM sgram GROUP BY 1, 2, 3),
shares AS (SELECT g.site, g.kind, g.i, LEAST(9, CAST(FLOOR(10 * COALESCE(c.k, 0) / n.n) AS INT64)) AS d
           FROM grams g JOIN n USING (site) LEFT JOIN cnt c ON c.site = g.site AND c.kind = g.kind AND c.g = g.g),
agg AS (SELECT site, kind, STRING_AGG(CAST(d AS STRING), '' ORDER BY i) AS shares FROM shares GROUP BY 1, 2)
SELECT s.site, s.kind, s.ord, n.n, COALESCE(s.chars, 0) AS chars, ARRAY_TO_STRING(s.w, ' ') AS words, COALESCE(a.shares, '') AS shares, s.sample
FROM seg s JOIN n USING (site) LEFT JOIN agg a USING (site, kind);
CREATE OR REPLACE TEMP TABLE pool AS
SELECT site, model, day, id, prompt_tokens, completion_tokens, duration_ms FROM call_rows
WHERE cost IS NOT NULL AND completion_tokens > 0
QUALIFY ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY FARM_FINGERPRINT(id)) <= {pool}
     OR ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY prompt_tokens, id) = 1
     OR ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY prompt_tokens DESC, id) = 1
     OR ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY completion_tokens DESC, id) <= 2
     OR ROW_NUMBER() OVER (PARTITION BY site, model ORDER BY duration_ms DESC, id) <= 2;
CREATE OR REPLACE TEMP TABLE coverage AS
SELECT c.*, COALESCE(p.placed, 0) AS placed, c.calls - c.other_format - c.repeated - COALESCE(p.placed, 0) AS unplaced
FROM cov0 c LEFT JOIN (SELECT usecase, day, COUNT(*) AS placed FROM call_rows GROUP BY 1, 2) p USING (usecase, day);
CREATE OR REPLACE TEMP TABLE leftover_count AS SELECT COUNT(*) AS n FROM calls c LEFT JOIN placed p USING (id) WHERE p.id IS NULL;
CREATE OR REPLACE TEMP TABLE leftover AS
SELECT c.id, c.usecase, c.day, c.model, c.prompt_tokens, c.cached AS cached_tokens, c.written AS written_tokens,
       c.completion_tokens, c.cost, c.cost_nocache
FROM calls c LEFT JOIN placed p USING (id) WHERE p.id IS NULL
QUALIFY ROW_NUMBER() OVER (PARTITION BY c.usecase, c.day ORDER BY FARM_FINGERPRINT(c.id)) <= {leftover};
"""

_DAYSITES = "SELECT * FROM day_sites"
_LEDGER = "SELECT * FROM ledger_daily"
_GAPS = "SELECT * FROM cache_gaps"
_LAYOUT = "SELECT * FROM site_layout"
_GRAMS = "SELECT * FROM site_grams"
_POOL = "SELECT * FROM pool"
_LEFTOVER = "SELECT * FROM leftover"
_LEFTOVER_COUNT = "SELECT n FROM leftover_count"
_COVERAGE = "SELECT * FROM coverage"
_SAMPLE = "SELECT sys, first_user, tool_names FROM site_calls WHERE site = @site AND rn = 1"
_SAMPLE_ID = "SELECT sys, first_user, tool_names FROM src WHERE id = @id AND chat LIMIT 1"
_SITE_CALLS = "SELECT site, usecase, id, model, sys, rn FROM site_calls WHERE site IN UNNEST(@sites)"
_CHAT_COUNTS = "SELECT usecase, COUNT(*) AS n FROM calls GROUP BY 1"


_USECASES = """
SELECT `{usecase}` AS usecase, COUNT(*) AS calls, MIN(DATE(`{time}`)) AS first_day, MAX(DATE(`{time}`)) AS last_day
FROM `{table}` WHERE DATE(`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY) AND {skip}
GROUP BY 1 ORDER BY calls DESC
"""


def _names():
    c = bq.cfg()
    model_col = credentials.get_config("BQ_MODEL_COLUMN")
    return {**c, "system": _SYSTEM.format(text=_TEXT, head_cap=HEAD_CAP, **c), "words_sys": _WORDS.format(text="sys"),
            "served": "CAST(`%s` AS STRING)" % model_col if model_col else "CAST(NULL AS STRING)", "skip": bq.SKIP_FILTER.format(col=c["usecase"]),
            "place": _PLACE, "prefix": PREFIX, "seeds": SEEDS, "heads": HEADS, "pairs": PAIRS, "pool": POOL, "leftover": LEFTOVER, "map_sample": MAP_SAMPLE, "head_cap": HEAD_CAP,
            "words_tools": _WORDS.format(text="tools_text"), "words_text": _WORDS.format(text="sys_text"), "words_user": _WORDS.format(text="first_user"),
            "cases": " ".join("WHEN gap <= %d THEN '%s'" % (secs, name) for name, secs in BUCKETS)}


def usecases(days=30):
    """Every usecase of the window with its calls (all formats) and days: the usecase and time columns only, cheap."""
    c = bq.cfg()
    return [dict(r) for r in bq.query(_USECASES.format(skip=bq.SKIP_FILTER.format(col=c["usecase"]), **c),
                                      {"days": days, **bq.filters()})]


def _prices():
    from app.catalog import PRICE
    return [{"model": m, "input": float(p["input"]), "cache_read": float(p["cache_read"]), "output": float(p["output"]),
             "cache_write": float(p.get("cache_write") or 0)} for m, p in PRICE.items()]


def _request(row):
    return {"messages": [{"role": "system", "content": row["sys"]}, {"role": "user", "content": row["first_user"] or ""}],
            "tools": [{"function": {"name": n}} for n in row["tool_names"] or []]}


_STEPS = (("src", "reading the day's calls"), ("cov0", "reading the day's calls"), ("calls", "reading the day's calls"),
          ("fp", "placing calls by the known fingerprints"), ("placed", "placing calls by the known fingerprints"),
          ("call_rows", "collecting the placed calls"), ("pairs", "cache prefix"), ("lcp", "cache prefix"), ("site_layout", "prompt layout"), ("site_grams", "prompt map"),
          ("prices", "totals"), ("ledger_daily", "totals"), ("cache_gaps", "cache gaps"), ("day_sites", "collecting the results"),
          ("site_calls", "collecting the results"), ("pool", "audit samples"), ("coverage", "coverage"), ("leftover", "unplaced calls"))


def _plain(text):
    """A script statement's head ("CREATE TEMP TABLE src AS (2 statements done, 1 min)") in plain words."""
    name = text.replace("CREATE OR REPLACE TEMP TABLE ", "").replace("CREATE TEMP TABLE ", "").replace("INSERT INTO ", "").split(" ")[0]
    tail = text[text.rfind("("):] if "(" in text else ""
    return "%s %s" % (next((w for k, w in _STEPS if name.startswith(k)), name), tail)


class Build:
    """The day script for days day_from..day_to (dates, inclusive): READ places the calls by the fingerprints known
    (known: [{usecase, site, markers, need}], phrases joined by '|'); the loop then uses seeds()/hits()/place();
    finish() computes the rest; the readers below download it."""
    def __init__(self, day_from, day_to, known=(), session_id=None, redo=False):
        """With `session_id`, the day's earlier session: finished (coverage there) -> resumed, nothing runs; else, or
        with `redo`, its read is kept (src there) and only the placement is redone; gone -> read afresh."""
        self.s = bq.Session(session_id) if session_id else bq.Session()
        self.s.day = str(day_from)
        self.resumed = bool(session_id) and not redo and self.s.has("coverage")
        self.timing = None
        if self.resumed:
            return
        known = [{"usecase": k["usecase"] or "", "site": str(k["site"]), "markers": k["markers"], "need": int(k.get("need") or 1)}
                 for k in known] or [{"usecase": "\x00", "site": "0", "markers": "", "need": 1}]   # a typed, empty array
        if session_id and self.s.has("src"):
            self._script(REPLACE, {"known": known}, day_from)
            return
        if session_id:                                      # the session is gone: start afresh
            self.s = bq.Session()
            self.s.day = str(day_from)
        self._script(READ, {"day_from": day_from, "day_to": day_to, "prices": _prices(), "known": known, **bq.filters()}, day_from)

    def _script(self, sql, params, day):
        from core import tasks
        self.s.script(sql.format(**_names()), params, progress=lambda text: tasks.note("%s: %s" % (day, _plain(text))))
        t = getattr(self.s, "timing", None)
        self.timing = "; ".join(x for x in (self.timing, t) if x) or None

    def finish(self, day):
        self._script(FINISH, {}, day)

    def _read(self, sql, params=None):
        return [dict(r) for r in self.s.query(sql, params, timeout=4 * 3600)]

    # ---- the loop's statements
    def chat_counts(self):
        return {r["usecase"]: r["n"] for r in self._read(_CHAT_COUNTS)}

    def seeds(self, live, rnd, asked=()):
        return self._read(_SEEDS.format(seeds=SEEDS, heads=HEADS), {"live": list(live), "round": rnd, "asked": list(asked) or [""]})

    def hits(self, cands):
        """cands: [{usecase, cand, phrases: [..], need}] -> ({cand: {free, collisions: [{site, n}]}}, [(a, b, n)])"""
        self.s.query(_HITS, {"cands": [{"usecase": c["usecase"], "cand": c["cand"], "phrases": "|".join(c["phrases"]), "need": int(c["need"])} for c in cands]})
        counts = {r["cand"]: {"free": r["free_ids"], "collisions": [{"site": x["site"], "n": x["n"]} for x in r["collisions"] if x["site"]]}
                  for r in self._read(_HIT_COUNTS)}
        return counts, [(r["a"], r["b"], r["n"]) for r in self._read(_OVERLAPS)]

    def place(self, accepted, rnd):
        """accepted: {cand: site} -> {site: calls placed in all}"""
        if not accepted:
            return {}
        self.s.query(_PLACE_ROUND, {"accepted": [{"cand": c, "site": s} for c, s in accepted.items()], "round": rnd})
        return {r["site"]: r["n"] for r in self._read(_SITE_COUNTS, {"sites": sorted(set(accepted.values()))})}

    def move(self, frm, to):
        """The calls placed in `frm` go to `to` (the reader found one template); -> calls now in `to`."""
        self.s.query(_MOVE, {"frm": frm, "to": to})
        return {r["site"]: r["n"] for r in self._read(_SITE_COUNTS, {"sites": [to]})}.get(to, 0)

    def site_heads(self, sites, rnd, not_ids=()):
        """HEADS calls placed this round in each of `sites` ({site: usecase}), the seeds left out, in one query (one
        per site took minutes a round: a session runs one query at a time) -> {site: [{sys, model}]}."""
        if not sites:
            return {}
        out = {site: [] for site in sites}
        for r in self._read(_SITE_HEADS.format(heads=HEADS), {"sites": [{"usecase": uc or "", "site": site, "rnd": rnd} for site, uc in sites.items()],
                                                              "not": list(not_ids) or [""]}):
            out[r["site"]].append({"sys": r["sys"], "model": r["model"]})
        return out

    # ---- the readers
    def day_sites(self):
        return self._read(_DAYSITES)

    def ledger(self):
        return self._read(_LEDGER)

    def gaps(self):
        return self._read(_GAPS)

    def layout(self):
        return {r["site"]: r for r in self._read(_LAYOUT)}

    def grams(self):
        """{site: {n, segments: [{kind, chars, words, shares, sample}]}}: the prompt map per day-site, tools, system,
        first message in order; `sample` is the raw text of the sample head's segment."""
        out = {}
        for r in sorted(self._read(_GRAMS), key=lambda r: (r["site"], r["ord"])):
            e = out.setdefault(r["site"], {"n": r["n"], "segments": []})
            e["segments"].append({"kind": r["kind"], "chars": r["chars"], "words": r["words"], "shares": r["shares"], "sample": r["sample"] or ""})
        return out

    def pool(self):
        return self._read(_POOL)

    def coverage(self):
        return self._read(_COVERAGE)

    def leftover(self):
        """(the unplaced calls, capped per usecase and day; how many there were in all)"""
        return self._read(_LEFTOVER), self._read(_LEFTOVER_COUNT)[0]["n"]

    def site_calls(self, sites):
        """The few calls kept per day-site (text included), for the given day-sites."""
        return self._read(_SITE_CALLS, {"sites": list(sites)}) if sites else []

    def sample_request(self, site=None, call_id=None):
        """One call (of a day-site, or by id) as a request body: system prompt, first user message, tool names."""
        rows = self._read(_SAMPLE, {"site": str(site)}) if site else self._read(_SAMPLE_ID, {"id": call_id})
        return _request(rows[0]) if rows else None

    def close(self):
        self.s.close()
