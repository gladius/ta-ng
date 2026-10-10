"""Downgrade detection: single-model call-sites a cheaper model might replace, and roughly what that saves.
Deterministic and free; mixed (routed) nodes are skipped, and the paid proof is prove.py."""
from app.catalog import PRICE, next_cheaper, open_weight_target, canonical_model
from app.migration import migration_target
from app.registry import is_available


def candidates(nodes, per_calls=1000, mode="commercial"):
    """Graph nodes -> net-saving candidates, ranked by $ saved per `per_calls` calls. 'commercial' prefers the vendor
    migration path, else the next cheaper same-provider model; 'open-weight' has no commercial fallback."""
    out = []
    for n in nodes:
        if n.get("mixed"):
            continue
        model = canonical_model(n["model"]) or n["model"]
        if mode == "open-weight":
            cheaper = open_weight_target(model, n["avg_in"], n["avg_out"], avail=is_available)
            reason, value = "open-weight", None
        else:
            mt = migration_target(model)
            if mt and is_available(mt["to"]):                 # only if the gateway serves it
                cheaper, reason, value = mt["to"], "migration", mt.get("value")
            else:
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
