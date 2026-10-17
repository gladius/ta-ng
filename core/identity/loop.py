"""The elimination loop: the calls no known fingerprint placed, usecase by usecase, seed by seed, until the usecase
is (nearly) empty.

A round, for every usecase still live: the day script hands over its biggest remaining keys (tool set + first PREFIX
masked characters) with HEADS calls each, or HEADS calls at random when no key is big enough. One model call per
seed reads the three heads whole and returns everything the call-site needs: a name and what it does (the profile),
PHRASES literal phrases of the fixed instructions (the fingerprint), the fixed and per-call regions (the cache view),
and whether these calls are a new version of a call-site the usecase already has. Nothing is trusted as said:
  - a phrase that is not in all three heads verbatim (masked) is dropped; under MIN_PHRASES left: asked once more;
    heads that are not one template (twins sharing their first lines) yield the first head's template alone;
  - the phrases are counted over the usecase's calls (build.hits): under MIN_CALLS free calls -> rejected (the model
    picked data, or the three calls were not one template); calls already placed in another call-site that they also
    hit -> a new version of it when it is most of that call-site, else asked once more for phrases that tell them apart;
  - two candidates of one round matching mostly the same calls are one template: the bigger stays.
The calls an accepted candidate matches are placed (build.place) and leave the pool; three of them, not the seeds, are
shown to the model once more ("one template?"): a no marks the version mixed (shown, never silent). The next round
seeds from what is left. A usecase is done under DONE_SHARE unplaced, or after GIVE_UP rounds that accepted nothing. The phrases, the
model's reason, the counts and the day are stored with the call-site: every grouping decision can be checked."""
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor

from core import tasks
from core.identity import store
from core.ledger import build

MIN_CALLS = build.MIN_CALLS
PHRASES = 5              # phrases asked for
MIN_PHRASES = 3          # phrases a fingerprint needs after the verbatim check
MAX_ROUNDS = 40
DONE_SHARE = 0.01        # a usecase is done when under this share of its chat calls is unplaced
GIVE_UP = 2              # rounds in a row accepting nothing -> the usecase is done (the rest stays unplaced)
OVERLAP = 0.5            # two candidates sharing this share of the smaller one's calls are one template
VERSION_OF = 0.5         # a candidate hitting this share of a known call-site's calls is its new version
WORKERS = 8
TASKS = ("classify", "extract", "answer", "draft", "summarise", "converse", "orchestrate", "other")
OUTPUTS = ("json", "text", "tool_calls", "label")

_PROMPT = """You are identifying a call-site: one place in a production system that sends a prompt to a model. Below
are the heads of {n} logged requests (the tool names, then the system prompt) that an auditor believes come from the
same call-site: the same fixed instructions, with per-call parts (names, ids, dates, account details, retrieved
documents, a user's text) that vary from call to call.
{known}
Return one json object:
- "same_template": true if all {n} are the same template with different per-call data, false if they are different
  templates. If false, everything below describes the template of REQUEST 1 ONLY (its phrases must be in request 1;
  they need not be in the others), so that its call-site is still made and the others are seeded again.
- "name": 2 to 4 words a person would call this call-site (e.g. "Bill explainer", "Claim eligibility check").
- "does": one sentence, plain words, what it does for whom.
- "task": one of {tasks}. "output": one of {outputs}.
- "phrases": {k} phrases copied VERBATIM from the fixed instructions, each 6 to 12 words, present in all {n} heads
  (in request 1 when same_template is false),
  as distinctive of this call-site as possible (not generic sentences like "you are a helpful assistant"), never
  from per-call data.{avoid}
- "fixed": one short phrase: what stays the same every call. "per_call": one short phrase: what changes, or "".
- "regions": the head of request 1 cut into at most 10 regions in order, each {{"kind": "fixed" or "per_call",
  "starts_with": the first 6 to 10 words of the region copied verbatim, "what": 3 to 6 words}}.
- "same_as": the id of the call-site listed above these requests are a new version of (same purpose, mostly the same
  instructions, some changed), else null. "same_as_why": one sentence, or "".
- "cache_note": one sentence on what would stop a provider's prompt cache (per-call text before the fixed
  instructions, a date at the top, ...), or "".
Only the json object.

{heads}"""
_KNOWN = "\nCall-sites this application already has (id: name, some of its phrases):\n{sites}\n"


def mask(text):
    """A text as the day script's masked words: lower case, numbers as #, letters and # only, one space between."""
    return " ".join(re.findall(r"[a-z#]+", re.sub(r"[0-9]+", "#", (text or "").lower().replace("#", " "))))


def site_id(usecase, phrases):
    key = (usecase or "") + "\x1f" + "|".join(sorted(phrases))
    return str(int.from_bytes(hashlib.sha1(key.encode()).digest()[:8], "big", signed=True))


def model():
    """AUDIT_PROFILE_MODEL (default claude-sonnet-5): the model that reads the heads."""
    import credentials
    return credentials.get_config("AUDIT_PROFILE_MODEL") or "claude-sonnet-5"


def ask(heads, known, avoid=()):
    """One model call -> the parsed json (raw)."""
    from app.services import llm_client
    known_txt = _KNOWN.format(sites="\n".join("- %s: %s; %s" % (s["site"], s["name"] or "(unnamed)", "; ".join('"%s"' % p for p in s["phrases"][:3]))
                                               for s in known[:40])) if known else ""
    avoid_txt = (" Not these, they are also in other call-sites' calls: %s." % "; ".join('"%s"' % p for p in avoid)) if avoid else ""
    body = "\n\n".join("--- request %d%s ---\n%s" % (i + 1, " (model %s)" % h["model"] if h.get("model") else "", h["sys"] or "") for i, h in enumerate(heads))
    prompt = _PROMPT.format(n=len(heads), known=known_txt, tasks=", ".join(TASKS), outputs=", ".join(OUTPUTS), k=PHRASES, avoid=avoid_txt, heads=body)
    for attempt in range(3):                      # an empty or cut reply happens now and then: asked again
        r = llm_client.complete(model=model(), max_tokens=4000, response_format={"type": "json_object"},
                                messages=[{"role": "user", "content": prompt}])
        txt = llm_client.text(r).strip()
        try:
            return json.loads(txt[txt.find("{"):txt.rfind("}") + 1], strict=False)
        except ValueError:
            if attempt == 2:
                raise


def check(answer, heads):
    """The model's phrases that are in every head verbatim (masked), masked, with at least three real words (a masked
    date, "today is # # #", is in every head too); and `need` (all but one of 4+). When the model says the heads are
    not one template, the phrases describe request 1 only and are checked against it alone."""
    masked = [" %s " % mask(h["sys"]) for h in (heads[:1] if answer.get("same_template") is False else heads)]
    kept = []
    for p in answer.get("phrases") or []:
        m = mask(p if isinstance(p, str) else "")
        if sum(1 for w in m.split() if w != "#") >= 3 and m not in kept and all(" %s " % m in x for x in masked):
            kept.append(m)
    return kept, (len(kept) - 1 if len(kept) >= 4 else len(kept))


def layout(answer, head):
    """From the model's regions on head 1: (fixed chars, per-call chars, fixed chars after the first per-call region),
    or None when no region's start is found. Regions whose start is not in the text are skipped; the rest are taken in
    the order they sit in the text."""
    head = re.sub(r"\s+", " ", head or "")                   # one space everywhere, as the starts are compared
    low = head.lower()
    starts = []
    for r in answer.get("regions") or []:
        if not isinstance(r, dict):
            continue
        s = re.sub(r"\s+", " ", str(r.get("starts_with") or "").lower()).strip()
        at = low.find(s[:40]) if s else -1
        if at >= 0:
            starts.append((at, r.get("kind") == "per_call"))
    starts = sorted(set(starts))
    if not starts:
        return None
    if starts[0][0] > 0:                                     # text before the first region found: fixed by default
        starts.insert(0, (0, False))
    fixed = after = 0
    first_dyn = next((at for at, dyn in starts if dyn), None)
    for i, (at, dyn) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(head)
        if not dyn:
            fixed += end - at
            if first_dyn is not None and at > first_dyn:
                after += end - at
    return fixed, len(head) - fixed, after


def mixed(heads):
    """The homogeneity check on calls a fingerprint placed: (mixed?, why) from one model call; (None, "") when it failed."""
    try:
        a = ask(heads, [])
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:120])
    return a.get("same_template") is False, str(a.get("same_as_why") or a.get("does") or "")[:200] if a.get("same_template") is False else ""


def _candidate(usecase, key, heads, known, avoid=()):
    """Ask the model about one seed -> a candidate dict, or None (not one template / too few phrases)."""
    try:
        a = ask(heads, known, avoid)
    except Exception as e:
        return {"usecase": usecase, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}
    phrases, need = check(a, heads)
    if len(phrases) < MIN_PHRASES and not avoid:               # asked once more, verbatim stressed by the drop
        try:
            a2 = ask(heads, known)
            p2, n2 = check(a2, heads)
            if len(p2) > len(phrases):
                a, phrases, need = a2, p2, n2
        except Exception:
            pass
    if len(phrases) < MIN_PHRASES:
        return None
    return {"usecase": usecase, "key": key, "cand": "%s|%s" % (usecase, key), "phrases": phrases, "need": need, "answer": a,
            "heads": heads, "same_as": str(a["same_as"]) if a.get("same_as") and str(a["same_as"]) in {s["site"] for s in known} else None}


def _record(c, site, day, placed, version_of=None, mixed_=(False, "")):
    a = c["answer"]
    lay = layout(a, c["heads"][0]["sys"])
    return {"site": site, "usecase": c["usecase"], "phrases": c["phrases"], "need": c["need"], "day": day, "placed": placed,
            "version_of": version_of, "reason": str(a.get("same_as_why") or "")[:300] if version_of else "",
            "mixed": bool(mixed_[0]), "mixed_why": mixed_[1] if mixed_[0] else "",
            "profile": {"name": str(a.get("name") or "")[:60], "does": str(a.get("does") or "")[:300],
                        "task": a.get("task") if a.get("task") in TASKS else "other",
                        "output": a.get("output") if a.get("output") in OUTPUTS else "text",
                        "fixed": str(a.get("fixed") or "")[:200], "per_call": str(a.get("per_call") or "")[:200], "model": model()},
            "regions": [{"kind": "per_call" if r.get("kind") == "per_call" else "fixed", "what": str(r.get("what") or "")[:60]}
                        for r in (a.get("regions") or []) if isinstance(r, dict)][:20],
            "cache_note": str(a.get("cache_note") or "")[:300],
            "static_chars": lay[0] if lay else None, "dynamic_chars": lay[1] if lay else None, "static_after": lay[2] if lay else None}


def run(b, day, known_of, candidate=_candidate, homogeneity=mixed):
    """The loop over the day's unplaced calls in session `b` (a build.Build after READ). known_of(usecase) -> the
    usecase's call-sites [{site, name, phrases, calls}]. -> {site: record} of the call-sites made or given a version
    this day (their calls are placed in the session). `candidate` is the model step (swapped in tests)."""
    chat = b.chat_counts()
    live = {uc for uc, n in chat.items() if n >= MIN_CALLS}
    stale, out, known = {}, {}, {}
    for rnd in range(1, MAX_ROUNDS + 1):
        if not live:
            break
        tasks.note("%s: round %d, %d usecases live" % (day, rnd, len(live)))
        rows = b.seeds(sorted(live), rnd)
        by_uc = {}
        for r in rows:
            by_uc.setdefault(r["usecase"], {}).setdefault(r["key"], []).append(r)
        jobs = []
        for uc in sorted(live):
            seeds = by_uc.get(uc) or {}
            left = next((rs[0]["n_left"] for rs in seeds.values() if rs), 0)
            if left < MIN_CALLS or left <= DONE_SHARE * chat[uc]:
                live.discard(uc)
                continue
            known.setdefault(uc, [{**s, "phrases": list(s["phrases"])} for s in known_of(uc)])
            big = [k for k, rs in seeds.items() if k is not None and rs and rs[0]["n"] >= MIN_CALLS]
            for k in (big or ([None] if None in seeds else [])):
                heads = [{"id": r["id"], "sys": r["sys"], "model": r["model"]} for r in seeds[k]][:build.HEADS]
                if len(heads) == build.HEADS:
                    jobs.append((uc, k, heads))
        if not jobs:
            break
        with ThreadPoolExecutor(WORKERS) as ex:
            cands = [c for c in ex.map(lambda j: candidate(j[0], j[1], j[2], known[j[0]]), jobs) if c]
        errors = [c for c in cands if c.get("error")]
        cands = [c for c in cands if not c.get("error")]
        failed = {c["usecase"] for c in errors}                  # a failed model call is not a round that found nothing
        if errors:
            tasks.note("%s: round %d, %d model calls failed: %s" % (day, rnd, len(errors), errors[0]["error"]))
        accepted, retry = _decide(b, cands, known, rnd) if cands else ({}, [])
        if retry:                                               # once more, told which phrases collide
            with ThreadPoolExecutor(WORKERS) as ex:
                again = [c for c in ex.map(lambda c: candidate(c["usecase"], c["key"], c["heads"], known[c["usecase"]], c["avoid"]), retry)
                         if c and not c.get("error")]
            if again:
                more, _ = _decide(b, again, known, rnd, final=True)
                accepted.update(more)
        placed = b.place({c: s for c, (s, _) in accepted.items()}, rnd)
        heads_of = {cand: b.site_heads(c["usecase"], site, rnd, [h.get("id") for h in c["heads"] if h.get("id")])
                    for cand, (site, c) in accepted.items()}    # one session query at a time (BigQuery allows no more)
        with ThreadPoolExecutor(WORKERS) as ex:                  # three placed calls, not the seeds: one template?
            checks = dict(zip(accepted, ex.map(lambda cand: homogeneity(heads_of[cand]) if len(heads_of[cand]) == build.HEADS else (False, ""),
                                               accepted)))
        touched = set()
        for cand, (site, c) in accepted.items():
            uc = c["usecase"]
            touched.add(uc)
            rec = _record(c, site, day, placed.get(site, 0), version_of=c.get("version_of"), mixed_=checks.get(cand) or (False, ""))
            out[site] = rec
            known[uc] = [s for s in known[uc] if s["site"] != site] + [{"site": site, "name": rec["profile"]["name"], "phrases": c["phrases"], "calls": placed.get(site, 0)}]
            store.save_site(rec)
        for uc in list(live):
            stale[uc] = 0 if uc in touched else stale.get(uc, 0) + (0 if uc in failed else 1)
            if stale[uc] >= GIVE_UP:
                live.discard(uc)
    return out


def _decide(b, cands, known, rnd, final=False):
    """Count every candidate's phrases over its usecase and decide: {cand: (site, candidate)} accepted, and the
    candidates to ask once more (their `avoid` phrases set)."""
    for c in cands:
        c["cand"] = "%s|%s|%d" % (c["usecase"], c["key"], rnd)
    counts, overlaps = b.hits(cands)
    by = {c["cand"]: c for c in cands}
    free = {k: v["free"] for k, v in counts.items()}
    drop = set()
    for a, bb, n in overlaps:                                  # one template seeded twice: the bigger stays
        if a in by and bb in by and n >= OVERLAP * min(free.get(a, 0), free.get(bb, 0)):
            drop.add(a if free.get(a, 0) < free.get(bb, 0) else bb)
    accepted, retry = {}, []
    for cand, c in by.items():
        if cand in drop or free.get(cand, 0) < MIN_CALLS:
            continue
        uc = c["usecase"]
        calls_of = {s["site"]: s.get("calls") or 0 for s in known.get(uc, [])}
        hit = sorted(counts[cand]["collisions"], key=lambda x: -x["n"])
        version_of = c.get("same_as")
        if hit and not version_of:
            top = hit[0]
            if top["n"] >= VERSION_OF * max(calls_of.get(top["site"], 0), 1):
                version_of = top["site"]
            elif not final:
                avoid = [p for p in c["phrases"] if any(p in s["phrases"] for s in known.get(uc, []) if s["site"] == top["site"])] or c["phrases"][:2]
                retry.append({**c, "avoid": avoid})
                continue
            else:
                continue                                         # still colliding: this seed stays unplaced
        site = version_of or site_id(uc, c["phrases"])
        accepted[cand] = (site, {**c, "version_of": version_of})
    return accepted, retry
