"""Downgrade detection — which single-model call-sites run on an expensive tier that a cheaper tier in the
SAME provider might replace, and roughly how much that would save. Deterministic, $0.

Scope on purpose: MIXED / routed nodes are skipped (already tiered → that's a routing audit, deferred). The
re-run PROOF (run the cheaper model on diverse inputs + judge drift) is a SEPARATE, paid step — not here.
"""
from app.catalog import PRICE, next_cheaper, canonical_model
from app.migration import migration_target
from app.registry import is_available


def candidates(nodes, per_calls=1000):
    """From graph nodes -> ranked change candidates. `per_calls` = the volume basis for the $ figure.

    Target selection PREFERS the vendor migration path (config/migrations/) when one matches the node's model —
    that is the recommended move regardless of provider (e.g. Claude/GPT/older-Gemini -> a Gemini GA target). The
    same-provider cost-downgrade (next_cheaper, node-aware) is the FALLBACK when no path matches. Each row carries
    `reason` ('migration' | 'cost-downgrade') so the report can frame it honestly. Only net-SAVING candidates are
    emitted here; a migration that is quality-only (equal/higher cost) is surfaced by the migration advisory, not
    as a saving."""
    out = []
    for n in nodes:
        if n.get("mixed"):
            continue                                          # routed/mixed -> routing audit, deferred
        model = canonical_model(n["model"]) or n["model"]
        mt = migration_target(model)                          # vendor migration path is the PREFERRED target
        if mt and is_available(mt["to"]):                     # ...but only if the gateway actually serves it
            cheaper, reason, value = mt["to"], "migration", mt.get("value")
        else:                                                 # fallback: node-aware same-provider drop, also gated
            cheaper = next_cheaper(model, n["avg_in"], n["avg_out"], avail=is_available)
            reason, value = "cost-downgrade", None
        p, cp = PRICE.get(model), (PRICE.get(cheaper) if cheaper else None)
        if not (cheaper and p and cp):
            continue
        save = ((n["avg_in"] * (p["input"] - cp["input"])
                 + n["avg_out"] * (p["output"] - cp["output"])) / 1e6) * per_calls
        if save <= 0:
            continue
        out.append({"key": n["key"], "node": n["node"], "model": model, "cheaper": cheaper,
                    "reason": reason, "value": value, "avg_out": n["avg_out"], "calls": n["calls"],
                    "usd": round(save, 2), "per_calls": per_calls})
    return sorted(out, key=lambda x: -x["usd"])
