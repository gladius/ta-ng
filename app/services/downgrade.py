"""Downgrade detection — which single-model call-sites run on an expensive tier that a cheaper tier in the
SAME provider might replace, and roughly how much that would save. Deterministic, $0.

Scope on purpose: MIXED / routed nodes are skipped (already tiered → that's a routing audit, deferred). The
re-run PROOF (run the cheaper model on diverse inputs + judge drift) is a SEPARATE, paid step — not here.
"""
from auditor.util import PRICE, next_cheaper, canonical_model


def candidates(nodes, per_calls=1000):
    """From graph nodes -> ranked downgrade candidates. `per_calls` = the volume basis for the $ figure."""
    out = []
    for n in nodes:
        if n.get("mixed"):
            continue                                          # routed/mixed -> routing audit, deferred
        model = canonical_model(n["model"]) or n["model"]
        cheaper = next_cheaper(model)
        p, cp = PRICE.get(model), (PRICE.get(cheaper) if cheaper else None)
        if not (cheaper and p and cp):
            continue
        save = ((n["avg_in"] * (p["input"] - cp["input"])
                 + n["avg_out"] * (p["output"] - cp["output"])) / 1e6) * per_calls
        if save <= 0:
            continue
        out.append({"key": n["key"], "node": n["node"], "model": model, "cheaper": cheaper,
                    "avg_out": n["avg_out"], "calls": n["calls"],
                    "usd": round(save, 2), "per_calls": per_calls})
    return sorted(out, key=lambda x: -x["usd"])
