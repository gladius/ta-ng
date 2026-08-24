"""Vendor MIGRATION PATHS — the recommended model to move to, regardless of what an agent runs today.

Loaded from config/migrations/<provider>.json (e.g. Google's "Transition today to 3.7 Flash" deck). Cross-provider
by design: Claude / GPT / older-Gemini sources all consolidate onto a current Gemini GA target. This is the
PREFERRED recommendation in the downgrade lever; the same-provider cost-downgrade (catalog.next_cheaper) is the
fallback. `from[].re` is a REGEX matched against the canonicalized model id, so 'families' (any Opus, any *-mini)
resolve without listing every id — FIRST match wins. Kept separate from app.catalog on purpose: pricing / tier /
lifecycle (retire_date) live there; the vendor migration recommendation lives here.
"""
import os
import re
import json

from app.catalog import canonical_model

_MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "migrations")


def _load_migration_paths(dir_=_MIGRATIONS_DIR):
    """[{provider, to, to_status, tier, value, from:[{re, label}]}] from config/migrations/*.json."""
    out = []
    if not os.path.isdir(dir_):
        return out
    for fn in sorted(os.listdir(dir_)):
        if not fn.endswith(".json"):
            continue
        try:
            data = json.load(open(os.path.join(dir_, fn), encoding="utf-8"))
        except Exception:
            continue
        for p in data.get("paths", []):
            if p.get("to") and p.get("from"):
                out.append({"provider": data.get("provider"), "to": p["to"], "to_status": p.get("to_status"),
                            "tier": p.get("tier"), "value": p.get("value"), "from": p["from"]})
    return out


_MIGRATION_PATHS = _load_migration_paths()


def migration_target(model):
    """The vendor's recommended migration target for a model (via family/regex match), or None. The audited id is
    canonicalized, then matched against each path's `from` regexes; FIRST match wins. Cross-provider by design."""
    if not model:
        return None
    canon = (canonical_model(model) or model).lower()
    for p in _MIGRATION_PATHS:
        for src in p["from"]:
            pat = src.get("re")
            if pat and re.search(pat, canon):
                return {"to": p["to"], "tier": p.get("tier"), "value": p.get("value"),
                        "to_status": p.get("to_status"), "matched": src.get("label") or pat}
    return None


def migration_paths():
    """All loaded migration paths (for the migration_path/<provider>.html view)."""
    return _MIGRATION_PATHS
