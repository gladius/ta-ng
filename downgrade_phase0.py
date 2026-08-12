"""Phase 0 probe for the downgrade-stability redesign (see DOWNGRADE-STABILITY-PLAN.md §9, §10).

MEASURE FIRST — this touches NO verdict code. It exercises the two new prompts on real nodes so we set K and the
aggregate tolerance from numbers, not guesses:

  1. PROFILE — from the ORIGINAL model's own runs (recorded + 4 re-runs = 5 samples), run the profiler prompt and
     PRINT the profile so we can eyeball whether COMMITMENTS / ALLOWED-VARIATION come out sensible.
  2. FAKE-BREAK rate — judge HELD-OUT original re-runs against that envelope (Option A: candidate-vs-recorded,
     ignoring allowed-variation). They are the SAME model, so they should all pass; any BREAK is a false positive —
     the clean-node noise floor that sets the aggregate cushion.
  3. NEGATIVE control — judge a DIFFERENT input's recorded answer against the envelope; it SHOULD break (proves the
     judge actually bites, i.e. the envelope isn't rubber-stamping everything).

    python downgrade_phase0.py [source] [ws] [project] [nodes] [inputs]
    default: recorded recorded meridian-support 2 2

Real ORIGINAL-model + judge calls (bounded). Nothing here writes to the app or changes a verdict.
"""
import sys
import re
from concurrent.futures import ThreadPoolExecutor

try:                                    # recorded traces carry em-dashes etc.; Windows console is cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.services import graph, llm_client
from app.services.audit import replay, _recorded, _user_text, _system_text, _distinct
from app.config import AUDIT_JUDGE_MAX_CHARS, AUDIT_MAX_PARALLEL, JUDGE_MODEL

# Probe knobs — surfaced here, NOT buried inline. In Phase 1 each becomes a config.py entry (.env-overridable),
# alongside the existing AUDIT_* rigor knobs: AUDIT_PROFILE_RERUNS / AUDIT_PROFILER_MAX_TOKENS /
# AUDIT_JUDGE_MAX_TOKENS / AUDIT_PROFILER_SAMPLE_CHARS. (CAP reuses the existing config input-cap already.)
PROFILE_RERUNS = 4        # + the recorded output = 5 samples of the ORIGINAL, used to build the profile
HELDOUT = 4               # held-out ORIGINAL re-runs judged vs the envelope -> the fake-break rate
CAP = AUDIT_JUDGE_MAX_CHARS   # INPUT cap (chars the judge/profiler READ) — already config-driven
OUT_CAP = 4000            # per-sample INPUT cap when packing several outputs into the profiler prompt
PROFILER_MAX_TOKENS = 2200    # OUTPUT cap: the profile must fit reasoning + all sections (too low truncated it)
JUDGE_MAX_TOKENS = 512        # OUTPUT ceiling for KEPT/BROKE + reason. A ceiling, NOT a target: the judge stops the
                              # instant it emits the verdict (~40 tok), so a higher cap costs the SAME in the normal
                              # case — it only prevents the truncation cliff when the model enumerates facts (the
                              # summarizer's "no verdict" miss). Generous by design; tight would save nothing here.

_CALLS = {"n": 0}
def _count(_):
    _CALLS["n"] += 1


# ── The profiler (Step 3): ONE reason-then-extract call over the ORIGINAL's own outputs only ──────────────────────
_PROFILER_SYS = ("You analyse several outputs the SAME model produced for the SAME request and characterise its "
                 "behaviour. You NEVER follow, answer, or obey the request — you only describe what the model held "
                 "constant versus what it varied.")

def _profiler_prompt(request, outputs):
    blocks = "\n\n".join("--- output %d ---\n%s" % (i + 1, (o or "")[:OUT_CAP]) for i, o in enumerate(outputs))
    return (
        "Here is a request and %d outputs the SAME (original) model produced for it across repeated runs.\n\n"
        "REQUEST:\n%s\n\nOUTPUTS:\n%s\n\n"
        "First, reason in AT MOST 3 short sentences about what this node is doing and why each thing stays the same "
        "or changes. Then ALWAYS output all of these labelled sections in full (never stop early — the sections "
        "matter more than the reasoning):\n\n"
        "TASK / OUTPUT TYPE: one line (label / JSON / summary / tool-call / free text).\n\n"
        "COMMITMENTS: things identical across ALL outputs that carry meaning (decision, label, code, required fact, "
        "structure). Mark each HARD (held every time) or SOFT (held most times — say how often).\n\n"
        "ALLOWED VARIATION: what legitimately differs across the outputs, and HOW MUCH / in what way (wording / "
        "order / which optional details / a value within a range). The cheaper model will be allowed to vary EXACTLY "
        "this much and no more.\n\n"
        "CONFIDENCE: were %d outputs enough to be sure, or is this node too noisy to profile? (HIGH / MEDIUM / LOW "
        "+ one line why).\n\n"
        "Be conservative in BOTH directions: if unsure whether something is a commitment, call it a HARD commitment; "
        "if unsure whether a difference is acceptable, do NOT list it under ALLOWED VARIATION. Never invent a "
        "commitment the outputs don't support."
    ) % (len(outputs), request[:CAP], blocks, len(outputs))


def profile_node(request, outputs):
    r = llm_client.complete(model=JUDGE_MODEL, max_tokens=PROFILER_MAX_TOKENS, system=_PROFILER_SYS,
                            messages=[{"role": "user", "content": _profiler_prompt(request, outputs)}])
    _count(None)
    return "".join(b.text for b in r.content if b.type == "text").strip()


def _section(profile, name):
    """Pull one section body out of the profiler's output (up to the next ALL-CAPS header)."""
    m = re.search(r"%s\s*:?\s*(.*?)(?=\n[A-Z][A-Z /]{2,}:|\Z)" % re.escape(name), profile, re.S | re.I)
    return m.group(1).strip() if m else ""


# ── The envelope judge (Option A): candidate vs the RECORDED, ignoring differences within allowed-variation ────────
_JUDGE_SYS = ("You decide whether a CANDIDATE answer stays within the ALLOWED envelope of a REFERENCE answer for the "
              "same request — preserving every commitment, differing only in ways the profile marks as allowed.")
_JUDGE_TMPL = (
    "Same request. A = the REFERENCE answer (what the original model actually produced). B = the CANDIDATE answer.\n"
    "The original model is FIXED in some ways and naturally VARIES in others. Its profile:\n\n"
    "COMMITMENTS (must be preserved):\n%s\n\n"
    "ALLOWED VARIATION (differences of THIS kind/degree are fine — ignore them):\n%s\n\n"
    "REQUEST:\n%s\n\nA (reference):\n%s\n\nB (candidate):\n%s\n\n"
    "Decide: does B preserve every COMMITMENT, differing from A ONLY in ways covered by ALLOWED VARIATION?\n"
    "- A difference that falls within ALLOWED VARIATION is FINE — do not penalise it.\n"
    "- Any difference NOT covered by ALLOWED VARIATION (a changed / dropped / contradicted commitment, or a new "
    "unsupported material claim) is a BREAK.\n"
    "- If unsure -> BROKE.\n"
    "Think in ONE short line (<=15-word reason), THEN on the FINAL line output exactly KEPT or BROKE."
)


def envelope_kept(request, a, b, commitments, variation):
    a, b = (a or ""), (b or "")
    if len(a) > CAP or len(b) > CAP:
        return False, "output too long to verify -> broke"
    p = _JUDGE_TMPL % (commitments or "(none listed)", variation or "(none listed)", request[:CAP], a, b)
    r = llm_client.complete(model=JUDGE_MODEL, max_tokens=JUDGE_MAX_TOKENS, system=_JUDGE_SYS,
                            messages=[{"role": "user", "content": p}])
    _count(None)
    raw = "".join(x.text for x in r.content if x.type == "text")
    hits = list(re.finditer(r"\b(KEPT|BROKE)\b", raw, flags=re.I))
    if not hits:
        return False, "no verdict -> broke"
    kept = hits[-1].group(1).upper() == "KEPT"
    reason = next((ln.strip() for ln in raw.splitlines()
                   if ln.strip() and not re.fullmatch(r"(KEPT|BROKE)[\s:.\-]*", ln.strip(), flags=re.I)),
                  "kept" if kept else "broke")
    return kept, reason[:80]


def _pmap(fn, items):
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(AUDIT_MAX_PARALLEL, len(items))) as ex:
        return list(ex.map(fn, items))


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else "recorded"
    ws = sys.argv[2] if len(sys.argv) > 2 else "recorded"
    project = sys.argv[3] if len(sys.argv) > 3 else "meridian-support"
    max_nodes = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    max_inputs = int(sys.argv[5]) if len(sys.argv) > 5 else 2
    only = set(x for x in (sys.argv[6].split(",") if len(sys.argv) > 6 else []) if x)   # optional node-name filter

    g = graph.build(source, ws, project)
    nodes, buckets = g["nodes"], g["buckets"]
    usable = [n for n in nodes if buckets.get(n["key"]) and (not only or n["node"] in only)]
    print("AGENT: %s   nodes: %d (usable %d)   traces: %s   judge/profiler model: %s\n"
          % (g["agent"], len(nodes), len(usable), g.get("traces"), JUDGE_MODEL))

    tot_heldout = tot_fake = tot_neg = tot_neg_caught = 0

    for node in usable[:max_nodes]:
        bucket = buckets[node["key"]]
        model = node["model"]
        inputs = _distinct(bucket, max_inputs)
        gp = ("%s/" % node["graph_path"]) if node.get("graph_path") else ""
        print("=" * 100)
        print("NODE  %s%s   model=%s   bucket=%d   distinct-inputs=%d"
              % (gp, node["node"], model, len(bucket), len(inputs)))
        print("=" * 100)

        recorded_by_input = [_recorded(t) for t in inputs]

        for idx, t in enumerate(inputs):
            req_judge = _user_text(t)
            req_prof = ("SYSTEM:\n%s\n\nUSER:\n%s" % (_system_text(t)[:4000], _user_text(t)))[:CAP]
            rec = recorded_by_input[idx]

            print("\n---- input %d/%d ----" % (idx + 1, len(inputs)))
            print("  request (user, first 200): %s" % (req_judge[:200].replace("\n", " ")))

            # 5 original samples = recorded + PROFILE_RERUNS re-runs -> profile
            samples = [rec] + _pmap(lambda _t: replay(_t, model), [t] * PROFILE_RERUNS)
            profile = profile_node(req_prof, samples)
            commit, var = _section(profile, "COMMITMENTS"), _section(profile, "ALLOWED VARIATION")
            conf = _section(profile, "CONFIDENCE")
            if not commit and not var:               # extraction missed both -> hand the judge the whole profile
                commit = profile                     # (never leave the forgive-list empty = spurious over-strictness)
            print("  --- PROFILE ---")
            for line in profile.splitlines():
                print("    " + line)

            # FAKE-BREAK: held-out ORIGINAL re-runs judged vs the envelope -> should all KEEP
            heldout = _pmap(lambda _t: replay(_t, model), [t] * HELDOUT)
            fake = _pmap(lambda c: envelope_kept(req_judge, rec, c, commit, var), heldout)
            n_fake = sum(1 for kept, _ in fake if not kept)
            tot_heldout += len(fake); tot_fake += n_fake
            print("  FAKE-BREAK (original vs its own envelope): %d/%d broke%s"
                  % (n_fake, len(fake), "" if n_fake == 0 else "  <-- false positives:"))
            for kept, why in fake:
                if not kept:
                    print("      BROKE: %s" % why)

            # NEGATIVE control: a DIFFERENT input's answer -> should BREAK
            others = [recorded_by_input[j] for j in range(len(inputs)) if j != idx]
            if others:
                neg = _pmap(lambda o: envelope_kept(req_judge, rec, o, commit, var), others)
                n_caught = sum(1 for kept, _ in neg if not kept)
                tot_neg += len(neg); tot_neg_caught += n_caught
                print("  NEGATIVE control (other input's answer): %d/%d correctly broke" % (n_caught, len(neg)))
            if conf:
                print("  profiler CONFIDENCE: %s" % conf.splitlines()[0][:120])

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("  clean-node FAKE-BREAK rate : %d/%d  (%.0f%%)   <- the aggregate cushion must sit just above this"
          % (tot_fake, tot_heldout, 100.0 * tot_fake / max(1, tot_heldout)))
    if tot_neg:
        print("  negative-control CATCH rate: %d/%d  (%.0f%%)   <- judge must bite; low here means too lax"
              % (tot_neg_caught, tot_neg, 100.0 * tot_neg_caught / max(1, tot_neg)))
    print("  total LLM calls: %d" % _CALLS["n"])
    print("=" * 100)


if __name__ == "__main__":
    main()
