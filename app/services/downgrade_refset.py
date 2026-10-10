"""Downgrade = the validation engine with ONE candidate per input, the cheaper model.

This maps the engine's result onto the rows the report renders and rolls the per-input verdicts up to the call-site:
an input is SAFE when the cheaper model stays within the original's own range ("within"); anything else (outside,
or unverified) is NOT-SAFE for the purpose of booking dollars. Node: SAFE if >= AUDIT_NODE_SAFE_RATIO of inputs SAFE.
"""
import math

from app.services.audit import _recorded, _user_text, _system_text
from app.config import (JUDGE_MODEL, AUDIT_JUDGE_MAX_CHARS, AUDIT_MAX_PARALLEL, AUDIT_PROFILE_RERUNS,
                        AUDIT_DOWNGRADE_K, AUDIT_JUDGE_VOTES, AUDIT_COHERENCE_FLOOR, AUDIT_DOWNGRADE_MARGIN,
                        AUDIT_NODE_SAFE_RATIO, AUDIT_JUDGE_MAX_TOKENS, AUDIT_JUDGE_REF_CHARS)
from validation import Spec, Item, Candidate, validate_many


def spec(k=None):
    """The app's audit settings as an engine Spec."""
    return Spec(reference_runs=AUDIT_PROFILE_RERUNS, candidate_runs=k or AUDIT_DOWNGRADE_K, votes=AUDIT_JUDGE_VOTES,
                coherence_floor=AUDIT_COHERENCE_FLOOR, margin=AUDIT_DOWNGRADE_MARGIN, judge_model=JUDGE_MODEL,
                judge_max_tokens=AUDIT_JUDGE_MAX_TOKENS, ref_chars=AUDIT_JUDGE_REF_CHARS,
                cap_chars=AUDIT_JUDGE_MAX_CHARS, max_parallel=AUDIT_MAX_PARALLEL)


def evidence_row(t, r, cap):
    """One engine result (its only candidate) -> the row the audit report's evidence shows."""
    c, ref = r["candidates"][0], r["reference"]
    return {"input": _user_text(t)[:cap], "system": _system_text(t)[:cap],
            "kept": c["kept"], "k": c["of"], "verdict": "SAFE" if c["verdict"] == "within" else "NOT-SAFE",
            "note": c["reason"], "unverified": c["verdict"] == "unverified",
            "self_rate": "%s/%d" % ("?" if ref["agree"] is None else ref["agree"], ref["of"]),
            "orig_runs": [{"preserved": x["agrees"], "reason": x["reason"], "votes": "",
                           "output": (x["output"] or "")[:cap]} for x in ref["runs"]],
            "samples": [{"preserved": x["kept"], "reason": x["reason"], "votes": x["votes"],
                         "output": (x["output"] or "")[:cap]} for x in c["runs"]]}


def prove_transform_refset(sample, cheaper, original, k=None):
    """Per sampled input: validate `cheaper` against `original`'s reference set. -> (rows, node_verdict, safe_count)."""
    s = spec(k)
    items = [Item(id=str(i), request=t, model=original, recorded=_recorded(t),
                  candidates=[Candidate("cheaper", cheaper)]) for i, t in enumerate(sample)]
    rows = [evidence_row(t, r, s.cap_chars) for t, r in zip(sample, validate_many(items, s))]
    safe = sum(1 for r in rows if r["verdict"] == "SAFE")
    need = math.ceil(AUDIT_NODE_SAFE_RATIO * len(rows)) if rows else 1
    return rows, ("SAFE" if rows and safe >= need else "NOT-SAFE"), safe
