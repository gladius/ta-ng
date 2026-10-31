"""The BigQuery pre-pass: call-sites found by statistics alone, placed as round -1 before the loop seeds; the loop then
describes each with one model call (loop.describe). Written by the other session, scored on the bench (FLOW.md E).

Both phases read only the calls no known fingerprint placed, in the session's `src` (build.READ ran) and `placed`:
  Phase 1, direct: a masked head repeated MIN_CALLS times or more is a call-site; its phrases are the most
    word-rich 4-grams of that one head (the top PHASE1_MAX heads per usecase).
  Phase 2, MinHash: a hash sample of the calls (SAMPLE_CALLS per usecase, 1-5%), one row per distinct head, 3-grams
    seen in 2+ heads of the usecase, 30 MinHash values, pairs sharing BANDS of them, one-pass union-find, the cluster
    split by its first 10 words; a cluster of 3+ heads whose first 80 characters agree is a call-site; its phrases
    are the 4-grams in 70% of its heads.
Every candidate is then proved the loop's way: counted over the usecase's calls (build.hits), accepted only with
MIN_CALLS free calls and no call already placed elsewhere, placed (build.place), saved (store.save_site) nameless
(profile model "bq_minhash"); loop.describe then names them. The loop treats them as known call-sites.
Known failure modes (bench): twin templates sharing their opening merge; a changing value in the first 80 characters
(a date first) yields no site, so those calls fall through to the loop."""
import re

from core import tasks
from core.identity import store
from core.ledger import build

MIN_CALLS = build.MIN_CALLS
MIN_PHRASES = 3
TOP_PHRASES = 5
PHASE1_MAX = 30          # repeated heads taken per usecase; the rest pile up at low counts (retrieved variants)
SAMPLE_CALLS = 500_000   # the sample aims at this many calls: 1% of a usecase of 50M, 5% under 10M
BANDS = 4                # MinHash values two heads must share (of 30): Jaccard about 0.13 and up

_PHASE1 = """
SELECT usecase, MIN(id) AS id, ANY_VALUE(norm) AS norm, COUNT(*) AS dup_count
FROM src s LEFT JOIN placed p USING (id)
WHERE s.chat AND p.id IS NULL AND s.usecase IN UNNEST(@live)
GROUP BY usecase, whole HAVING COUNT(*) >= @min_calls
QUALIFY ROW_NUMBER() OVER (PARTITION BY usecase ORDER BY dup_count DESC, id) <= @cap
"""

_PHASE2 = f"""
-- one row per distinct head of the sample (@rates: usecase, pct), its first 10 words
CREATE OR REPLACE TEMP TABLE _mh_reps AS
SELECT usecase, id, norm, dup_count,
       ARRAY_TO_STRING(ARRAY(SELECT w FROM UNNEST(REGEXP_EXTRACT_ALL(norm, r'[a-z#]+')) w WITH OFFSET o WHERE o < 10 ORDER BY o), ' ') AS prefix10
FROM (
  SELECT s.usecase, MIN(s.id) AS id, ANY_VALUE(s.norm) AS norm, COUNT(*) AS dup_count
  FROM src s LEFT JOIN placed p USING (id) JOIN UNNEST(@rates) r ON r.usecase = s.usecase
  WHERE s.chat AND p.id IS NULL AND ABS(MOD(FARM_FINGERPRINT(s.id), 100)) < r.pct
  GROUP BY s.usecase, s.whole);

-- MinHash over the 3-grams seen in 2+ heads of the usecase; pairs sharing {BANDS} of 30 values; one-pass union-find
CREATE OR REPLACE TEMP TABLE _mh_cs AS
WITH ngrams3 AS (
  SELECT DISTINCT usecase, id, gram FROM _mh_reps, UNNEST(ML.NGRAMS(REGEXP_EXTRACT_ALL(norm, r'[a-z#]+'), [3, 3])) AS gram),
freq AS (SELECT usecase, gram, COUNT(DISTINCT id) AS n FROM ngrams3 GROUP BY 1, 2),
filtered AS (SELECT n.usecase, n.id, n.gram FROM ngrams3 n JOIN freq f ON f.usecase = n.usecase AND f.gram = n.gram WHERE f.n >= 2),
minhash AS (
  SELECT usecase, id, k, MIN(FARM_FINGERPRINT(CONCAT(CAST(k AS STRING), '|', gram))) AS mh
  FROM filtered, UNNEST(GENERATE_ARRAY(1, 30)) AS k GROUP BY 1, 2, 3),
pairs AS (
  SELECT a.usecase, a.id AS id1, b.id AS id2
  FROM minhash a JOIN minhash b ON a.usecase = b.usecase AND a.k = b.k AND a.mh = b.mh AND a.id < b.id
  GROUP BY 1, 2, 3 HAVING COUNT(*) >= {BANDS}),
labeled AS (SELECT usecase, id1 AS member, id1 AS mhc FROM pairs UNION ALL SELECT usecase, id2, id1 FROM pairs),
mh_clust AS (SELECT usecase, member, MIN(mhc) AS mhc FROM labeled GROUP BY 1, 2)
SELECT r.usecase, r.id, r.norm, r.dup_count, r.prefix10,
       FARM_FINGERPRINT(CONCAT(CAST(COALESCE(c.mhc, r.id) AS STRING), '|', r.prefix10)) AS sk
FROM _mh_reps r LEFT JOIN mh_clust c ON c.usecase = r.usecase AND c.member = r.id;

-- clusters of 3+ heads agreeing on their first 80 characters; phrases: 4-grams with 3+ real words in 70% of the heads, top 5
CREATE OR REPLACE TEMP TABLE _mh_ph AS
WITH purity AS (SELECT usecase, sk, COUNT(*) AS sz, COUNT(DISTINCT SUBSTR(norm, 1, 80)) AS dp FROM _mh_cs GROUP BY 1, 2),
clean AS (SELECT usecase, sk, sz FROM purity WHERE sz >= 3 AND dp = 1),
c4 AS (
  SELECT cs.usecase, cs.sk, gram4, COUNT(DISTINCT cs.id) AS nm, ANY_VALUE(cl.sz) AS ssz
  FROM _mh_cs cs JOIN clean cl ON cl.usecase = cs.usecase AND cl.sk = cs.sk,
  UNNEST(ML.NGRAMS(REGEXP_EXTRACT_ALL(cs.norm, r'[a-z#]+'), [4, 4])) AS gram4
  WHERE (SELECT COUNT(*) FROM UNNEST(SPLIT(gram4, ' ')) w WHERE w != '#') >= 3     -- word-rich, like phase 1 and the loop's check
  GROUP BY 1, 2, 3)
SELECT usecase, sk, ANY_VALUE(ssz) AS sz, ARRAY_AGG(gram4 ORDER BY nm DESC, gram4 LIMIT {TOP_PHRASES}) AS phrases
FROM c4 WHERE nm >= 0.7 * ssz GROUP BY 1, 2 HAVING COUNT(*) >= {MIN_PHRASES};
"""
_PHASE2_READ = "SELECT usecase, phrases, sz FROM _mh_ph ORDER BY usecase, sz DESC"


def need(phrases):
    return len(phrases) - 1 if len(phrases) >= 4 else len(phrases)


def phrases(norm, k=TOP_PHRASES):
    """k word-rich 4-grams of one masked head (phase 1), spread over its length: a per-call value near the start
    then touches one phrase of five, and the other repeated heads of the template still match `need` of them."""
    words = re.findall(r"[a-z#]+", norm)
    grams = [" ".join(words[i:i + 4]) for i in range(max(0, len(words) - 3))]
    rich = [g for g in grams if sum(1 for w in g.split() if w != "#") >= 3]
    if len(rich) <= k:
        return rich
    picks = []
    for j in range(k):
        g = rich[int((j + 0.5) * len(rich) / k)]
        if g not in picks:
            picks.append(g)
    return picks


def sample_pct(calls):
    return max(1, min(5, int(SAMPLE_CALLS / max(1, calls) * 100)))


OVERLAP = 0.5            # two candidates sharing this share of the smaller one's calls are one template (as the loop)


def pick(cands, counts, overlaps=()):
    """The candidates proved by count: MIN_CALLS free calls, no call already placed in another call-site, and of two
    candidates matching mostly the same calls (one template with several repeated heads) only the bigger."""
    free = {k: v.get("free", 0) for k, v in counts.items()}
    drop = set()
    for a, b, n in overlaps:
        if n >= OVERLAP * min(free.get(a, 0), free.get(b, 0)):
            drop.add(a if free.get(a, 0) < free.get(b, 0) else b)
    return [c for c in cands if c["cand"] not in drop and free.get(c["cand"], 0) >= MIN_CALLS and not counts.get(c["cand"], {}).get("collisions")]


def record(c, day, placed):
    return {"site": c["cand"], "usecase": c["usecase"], "phrases": c["phrases"], "need": c["need"], "day": str(day), "placed": placed,
            "version_of": None, "reason": "", "mixed": False, "mixed_why": "",
            "profile": {"name": "", "does": "", "task": "other", "output": "text", "fixed": "", "per_call": "", "model": "bq_minhash"},
            "regions": [], "cache_note": "", "static_chars": None, "dynamic_chars": None, "static_after": None}


def run(b, live, day):
    """Both phases over the live usecases in session `b`; -> {site: record}, each saved and placed as round -1."""
    from core.identity.loop import site_id
    live = sorted(live)
    if not live:
        return {}
    chat = b.chat_counts()
    cands, seen = [], set()

    def add(uc, ph):
        sid = site_id(uc, ph)
        if len(ph) >= MIN_PHRASES and sid not in seen:
            seen.add(sid)
            cands.append({"usecase": uc, "cand": sid, "phrases": ph, "need": need(ph)})

    tasks.note("%s: pre-pass, phase 1 (repeated heads)" % day)
    for r in b._read(_PHASE1, {"live": live, "min_calls": MIN_CALLS, "cap": PHASE1_MAX}):
        add(r["usecase"], phrases(r["norm"]))
    n1 = len(cands)
    tasks.note("%s: pre-pass, phase 2 (MinHash on a sample); %d candidates so far" % (day, n1))
    b.s.script(_PHASE2, {"rates": [{"usecase": uc, "pct": sample_pct(chat.get(uc, 0))} for uc in live]})
    for r in b._read(_PHASE2_READ):
        add(r["usecase"], list(r["phrases"] or []))
    if not cands:
        tasks.note("%s: pre-pass found nothing" % day)
        return {}
    tasks.note("%s: pre-pass, %d candidates (%d repeated heads, %d clusters), counting" % (day, len(cands), n1, len(cands) - n1))
    counts, overlaps = b.hits(cands)
    accepted = pick(cands, counts, overlaps)
    placed = b.place({c["cand"]: c["cand"] for c in accepted}, -1)
    out = {}
    for c in accepted:
        if placed.get(c["cand"], 0) < MIN_CALLS:
            continue
        rec = record(c, day, placed[c["cand"]])
        out[c["cand"]] = rec
        store.save_site(rec)
    tasks.note("%s: pre-pass placed %d calls in %d call-sites (round -1)" % (day, sum(r["placed"] for r in out.values()), len(out)))
    return out
