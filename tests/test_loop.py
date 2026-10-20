"""The elimination loop (core/identity/loop.py) offline: a fake day (calls in memory standing in for the BigQuery
session) and a fake model that copies common windows of the three heads. The loop must make one call-site per
template, place the calls by proved phrases, leave the rare rest unplaced, see a changed template as a version of a
known call-site, and drop a phrase the model did not copy verbatim."""
import random
import types

from sqlalchemy import delete

from app.services import db
from core.identity import loop, store

A = ("You are the billing assistant for Acme. Today is {date}. Customer {name} (account {acct}) asks about a charge. "
     "Explain every line of the bill in plain words, quote the amounts exactly, and offer the payment plan when the "
     "balance is over 100 dollars. Never promise a refund without a case number.")
B = ("Classify the support ticket below into one of: outage, billing, device, other. Return the label only. "
     "Ticket from {name}: {text}")
C = ("You are the retention assistant for Acme. Customer {name} wants to cancel. Offer the loyalty discount first, "
     "then the downgrade, and log the reason with the retention tool. Today is {date}.")
A2 = A.replace("Explain every line of the bill in plain words,", "Walk through each charge on the invoice,")   # two phrases of five break
NAMES = ["Ann Lee", "Bob Ray", "Cy Ng"]


def _calls(template, n, usecase="billing", tools="", tag=""):
    rng = random.Random(n)
    out = []
    for i in range(n):
        sys = ("tools: %s\n" % tools if tools else "") + template.format(date="2026-10-%02d" % (1 + i % 28), name=rng.choice(NAMES),
                                                                          acct=rng.randint(10000, 99999), text=rng.choice(["my phone is dead", "double charged", "no signal"]))
        out.append({"id": "%s%s-%d" % (tag, template[:6], i), "usecase": usecase, "sys": sys, "model": "gpt", "norm": " %s " % loop.mask(sys)})
    return out


class Day:
    """The day's calls as the session holds them, with the loop's four statements in Python."""
    def __init__(self, calls, known=()):
        self.calls = {c["id"]: c for c in calls}
        self.placed = {}                                     # id -> site
        self.s = types.SimpleNamespace(totals={"jobs": 0, "s": 0.0, "bytes": 0, "slot_ms": 0})   # the session's job totals
        for k in known:
            for c in calls:
                if c["usecase"] == k["usecase"] and sum(" %s " % p in c["norm"] for p in k["markers"].split("|")) >= k["need"]:
                    self.placed[c["id"]] = k["site"]

    def chat_counts(self):
        out = {}
        for c in self.calls.values():
            out[c["usecase"]] = out.get(c["usecase"], 0) + 1
        return out

    def seeds(self, live, rnd, asked=()):
        rows = []
        for uc in live:
            left = [c for c in self.calls.values() if c["usecase"] == uc and c["id"] not in self.placed]
            keys = {}
            for c in left:
                keys.setdefault(c["norm"][:300], []).append(c)
            for k, cs in sorted(keys.items(), key=lambda kv: -len(kv[1]))[:loop.build.SEEDS]:
                rows += [{"usecase": uc, "key": hash(k), "n": len(cs), "static": False, "n_left": len(left), "id": c["id"], "sys": c["sys"],
                          "tool_names": [], "model": c["model"]} for c in sorted(cs, key=lambda c: c["id"] in asked)[:loop.build.HEADS]]
            rng = random.Random(rnd)
            pool = [c for c in left if c["id"] not in asked]
            rows += [{"usecase": uc, "key": None, "n": None, "static": None, "n_left": len(left), "id": c["id"], "sys": c["sys"], "tool_names": [],
                      "model": c["model"]} for c in rng.sample(pool, min(loop.build.HEADS, len(pool)))]
        return rows

    def _hits(self, c):
        return [x for x in self.calls.values() if x["usecase"] == c["usecase"] and sum(" %s " % p in x["norm"] for p in c["phrases"]) >= c["need"]]

    def hits(self, cands):
        self.cands = {c["cand"]: c for c in cands}
        counts, sets = {}, {}
        for c in cands:
            h = self._hits(c)
            sets[c["cand"]] = {x["id"] for x in h if x["id"] not in self.placed}
            col = {}
            for x in h:
                if x["id"] in self.placed:
                    col[self.placed[x["id"]]] = col.get(self.placed[x["id"]], 0) + 1
            counts[c["cand"]] = {"free": len(sets[c["cand"]]), "collisions": [{"site": s, "n": n} for s, n in col.items()]}
        overlaps = [(a, b, len(sets[a] & sets[b])) for a in sets for b in sets if a < b and sets[a] & sets[b]]
        return counts, overlaps

    def site_heads(self, sites, rnd, not_ids=()):
        return {site: [{"sys": c["sys"], "model": c["model"]} for i, c in self.calls.items() if self.placed.get(i) == site and i not in not_ids][:loop.build.HEADS]
                for site in sites}

    def place(self, accepted, rnd):
        for cand, site in accepted.items():
            for x in self._hits(self.cands[cand]):
                if x["id"] not in self.placed:
                    self.placed[x["id"]] = site
        out = {}
        for s in self.placed.values():
            out[s] = out.get(s, 0) + 1
        return {s: out.get(s, 0) for s in set(accepted.values())}


def fake_model(usecase, key, heads, known, avoid=()):
    """Copies 5-word windows that all heads share (skipping numbers, names and the ones to avoid, as a model told to
    keep off per-call data would), names the site after its first words."""
    words = [loop.mask(h["sys"]).split() for h in heads]
    names = {w for n in NAMES for w in n.lower().split()}
    common = [" ".join(words[0][i:i + 5]) for i in range(len(words[0]) - 4)]
    common = [w for w in common if "#" not in w and not names & set(w.split()) and all(" %s " % w in " %s " % " ".join(x) for x in words[1:]) and w not in avoid]
    if len(common) < 3:
        return None
    picked = [common[i] for i in range(0, len(common), max(1, len(common) // 5))][:5]
    same_as = next((s["site"] for s in known if sum(" %s " % p in " %s " % " ".join(words[0]) for p in s["phrases"]) >= 2), None)
    answer = {"same_template": True, "name": " ".join(words[0][2:4]).title(), "does": "test", "task": "answer", "output": "text",
              "phrases": picked + ["not in the text at all"], "fixed": "instructions", "per_call": "name, date",
              "regions": [{"kind": "fixed", "starts_with": " ".join(heads[0]["sys"].split()[:6]), "what": "instructions"}],
              "same_as": same_as, "same_as_why": "same instructions, one rule changed" if same_as else "", "cache_note": ""}
    phrases, need = loop.check(answer, heads)
    return {"usecase": usecase, "key": key, "cand": "", "phrases": phrases, "need": need, "answer": answer, "heads": heads, "same_as": same_as}


def _clean():
    with db.ready().begin() as c:
        c.execute(delete(store.callsites))


def test_the_loop_makes_one_call_site_per_template_and_leaves_the_rare_rest():
    _clean()
    day = Day(_calls(A, 200, tag="a") + _calls(B, 50, tools="classify", tag="b") + _calls(C, 30, tag="c") + _calls(B, 3, usecase="other", tag="o"))
    asked = []
    made = loop.run(day, "2026-10-10", store.brief, prepass=lambda *a: {}, candidate=fake_model, homogeneity=lambda heads: (asked.append(heads), (False, ""))[1])
    assert len(made) == 3 and sorted(r["placed"] for r in made.values()) == [30, 50, 200]
    assert len(asked) == 3 and all(len(h) == 3 for h in asked)         # every new call-site checked on three other calls
    assert len(day.placed) == 280                                   # "other": 3 calls, under MIN_CALLS, never seeded
    for rec in made.values():
        assert 3 <= len(rec["phrases"]) <= 5 and all("#" not in p for p in rec["phrases"])   # fixed text only
        assert rec["profile"]["name"] and rec["static_chars"] is not None
    sites = store.by_usecase("billing")
    assert len(sites) == 3 and all(s["versions"][0]["placed"] == s["calls"] for s in sites)
    fps = store.fingerprints()
    assert len(fps) == 3 and all(fp["need"] == len(fp["markers"].split("|")) - 1 for fp in fps if fp["markers"].count("|") >= 3)


def test_a_changed_template_becomes_a_version_of_the_known_call_site():
    _clean()
    day1 = Day(_calls(A, 100))
    made = loop.run(day1, "2026-10-10", store.brief, prepass=lambda *a: {}, candidate=fake_model, homogeneity=lambda heads: (True, "two different prompts"))
    (site, rec), = made.items()
    assert rec["mixed"] and store.get(site)["versions"][0]["mixed_why"] == "two different prompts"   # a no is shown, never silent
    known = store.fingerprints()
    day2 = Day(_calls(A, 60) + _calls(A2, 40, tag="v2"), known)   # the old calls placed by the known fingerprint
    assert len(day2.placed) == 60
    made2 = loop.run(day2, "2026-10-11", store.brief, prepass=lambda *a: {}, candidate=fake_model, homogeneity=lambda heads: (False, ""))
    assert len(day2.placed) == 100
    assert list(made2) == [site] and made2[site]["version_of"] == site          # the same call-site, a new version
    s = store.get(site)
    assert len(s["versions"]) == 2 and s["versions"][1]["first"] == "2026-10-11" and s["versions"][1]["reason"]
    assert len(store.fingerprints()) == 2                                      # both versions place from now on


def test_phrases_are_kept_only_when_in_every_head_verbatim():
    heads = [{"sys": "Today is 2026-10-01. Quote the amounts exactly and offer the plan."},
             {"sys": "Today is 2026-10-02. Quote the amounts exactly and offer the plan."},
             {"sys": "Today is 2026-10-03. Quote the amounts exactly and offer the plan."}]
    phrases, need = loop.check({"phrases": ["Quote the amounts exactly", "offer the plan", "today is 2026-10-01", "not there", "Quote the  amounts exactly"]}, heads)
    assert phrases == ["quote the amounts exactly", "offer the plan"] and need == 2   # the date is data; a duplicate once
    assert loop.check({"phrases": ["a b"]}, heads) == ([], 0)                        # two words are not a phrase


def test_the_layout_comes_from_the_models_regions_on_the_first_head():
    head = "Today is 2026-10-01. " + "Quote the amounts exactly and offer the plan. " * 3 + "Customer: Ann Lee, account 123."
    a = {"regions": [{"kind": "per_call", "starts_with": "Today is 2026-10-01."}, {"kind": "fixed", "starts_with": "Quote the amounts exactly and offer"},
                     {"kind": "per_call", "starts_with": "Customer: Ann Lee"}]}
    fixed, dyn, after = loop.layout(a, head)
    assert fixed == len("Quote the amounts exactly and offer the plan. " * 3) and after == fixed and fixed + dyn == len(head)
    assert loop.layout({"regions": [{"kind": "fixed", "starts_with": "nowhere in the text"}]}, head) is None


def test_circuit_breaker_failing_calls_end_the_usecase(monkeypatch):
    """A seed is asked once; a usecase whose model calls keep failing gives up after GIVE_UP rounds, not at the cap."""
    _clean()
    day = Day(_calls(A, 60, tag="a") + _calls(B, 40, usecase="claims", tag="b"))
    asked, notes = [], []

    def broken(usecase, key, heads, known, avoid=()):
        asked.append(tuple(h["id"] for h in heads))
        return {"usecase": usecase, "error": "ValueError: no json object in the reply"}
    monkeypatch.setattr(loop.tasks, "note", notes.append)
    assert loop.run(day, "2026-10-10", store.brief, candidate=broken, prepass=lambda *a: {}) == {}
    assert len(asked) == len(set(asked)), "a seed was asked twice"
    assert sum("usecases live" in n for n in notes) == loop.GIVE_UP
    assert sum("gave up" in n for n in notes) == 2 and any("loop done" in n for n in notes)


def test_circuit_breaker_counts_calls_left_not_what_the_loop_accepted(monkeypatch):
    """Call-sites accepted every round but no call leaves the pool (a placement that places nothing): the usecase is
    out after GIVE_UP rounds, measured on the unplaced calls, never left to run to MAX_ROUNDS."""
    _clean()
    day = Day(_calls(A, 200, tag="a") + _calls(B, 50, tools="classify", tag="b"))
    monkeypatch.setattr(day, "place", lambda accepted, rnd: {s: 0 for s in accepted.values()})
    notes, asked = [], []
    monkeypatch.setattr(loop.tasks, "note", notes.append)

    def model(*a, **k):
        asked.append(1)
        return fake_model(*a, **k)
    loop.run(day, "2026-10-10", store.brief, candidate=model, homogeneity=lambda h: (False, ""), prepass=lambda *a: {})
    assert sum("usecases live" in n for n in notes) == loop.GIVE_UP
    assert any("gave up: %d rounds took no calls" % loop.GIVE_UP in n for n in notes)
    assert len(asked) <= loop.GIVE_UP * loop.build.SEEDS * 3


def test_the_log_lines_pool_notes_errors_calls_and_bigquery_labels():
    """A pool thread writes to the task that started it; errors group by type; calls and jobs sum as the log says."""
    from core import tasks
    from core.ledger import bq
    tasks._current.task_id = "tsk_x"
    try:
        seen = loop._map(lambda _: tasks._current.task_id, range(4))
    finally:
        tasks._current.task_id = None
    assert seen == ["tsk_x"] * 4
    line = loop.errors_line(["APITimeoutError: Request timed out.", "ValueError: no json object (finish length)", "APITimeoutError: Request timed out."])
    assert line.startswith("2 APITimeoutError (Request timed out.); 1 ValueError")
    calls = [{"s": 20, "in": 9000, "out": 900, "finish": "stop", "error": None, "cost": 0.04},
             {"s": 180, "in": 0, "out": 0, "finish": None, "error": "APITimeoutError: x", "cost": 0}]
    assert loop.calls_line(calls) == "2 model calls (1 failed), median 180 s, slowest 180 s, 9,000 tokens in, 900 out, $0.04"
    assert loop.bq_line({"jobs": 5, "s": 120, "bytes": 3e9, "slot_ms": 0}, {"jobs": 2, "s": 60, "bytes": 1e9, "slot_ms": 0}) == \
        "BigQuery 3 jobs, 1.0 min, 2.0 GB read"
    assert bq.label("-- 7b. the prompt map\nCREATE TEMP TABLE x AS SELECT 1") == "7b. the prompt map"
    assert bq.label("CREATE OR REPLACE TEMP TABLE cand AS SELECT * FROM UNNEST(@cands)") == "cand AS"
