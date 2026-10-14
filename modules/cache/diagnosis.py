"""Why a call-site isn't caching more: one cause from a fixed list, with the evidence and what the fix is worth.

  ok               reads already cover most of what the fixed part allows
  no_mark          Claude without cache_control: nothing caches until the fixed block is marked (safe fix)
  below_min        the fixed part is under the provider's minimum: nothing to cache (no fix; maybe merge prompts)
  dynamic_first    per-call content sits before the fixed part, so the prefix changes every call (prompt change: proof)
  tools_vary       the tool list changes per call and tools lead the prefix (prompt change: a stable tool set)
  loss             caching costs more than it saves (writes with a surcharge, rarely read): stop marking (safe fix)
  gaps             the prefix repeats but calls arrive after it expired: a longer lifetime pays (safe fix)
  sparse           calls arrive after the prefix expired and the provider offers no longer lifetime
  versions_churn   the fixed part changed often in the window: each version restarts the cache
  unexplained      a constant fixed part, repeated often, yet few reads: look at the rows
"""
from modules.cache import rules

OK_SHARE = 0.6          # reads / achievable at or above this: ok
CHURN_VERSIONS = 5
MATERIAL = 0.02        # a lifetime change must save at least this share of the site's cost


def tokens(chars):
    return chars // 4


def diagnose(site, ledger, ttl):
    """site: a `sites` row; ledger: summed ledger_daily for the site+model; ttl: ttl.what_if() or None.
    -> {cause, evidence, achievable_share, saving, suggestion, proof}. Dollar figures are for the window."""
    model = ledger["model"]
    prov, r, cmin = rules.for_model(model)
    calls, ptok, cached = ledger["calls"], ledger["prompt_tokens"] or 0, ledger["cached_tokens"] or 0
    static_tok = tokens(site["static_chars"])
    fixed_now = int(static_tok * (1 - site["static_after_dynamic"]))      # fixed text already leading the prompt
    avg_prompt = ptok / calls if calls else 0
    share_now = cached / ptok if ptok else 0.0
    achievable = min(1.0, static_tok / avg_prompt) if avg_prompt else 0.0
    price = _price(model)
    unit = (price["input"] * (1 - r["read"]) / 1e6) if price else 0.0      # $ saved per token read instead of paid
    ev = {"provider": prov, "calls": calls, "avg_prompt_tokens": round(avg_prompt), "fixed_tokens": static_tok,
          "fixed_leading_tokens": fixed_now, "cache_min": cmin, "cached_share": round(share_now, 3),
          "achievable_share": round(achievable, 3), "tool_sets": site["tool_sets"],
          "versions": len(site.get("versions") or []), "static_after_dynamic": round(site["static_after_dynamic"], 2)}
    # a layout fix or a mark lets every call that reads nothing today read the fixed part
    saving = round(static_tok * max(0, calls - (ledger.get("calls_cached") or 0)) * unit, 2)
    reads_share = (ledger.get("calls_cached") or 0) / calls if calls else 0.0
    sparse = bool(ttl and ttl["calls"] and ttl["beyond_default"] > 0.5 * ttl["calls"])

    if static_tok < cmin and reads_share < 0.05:                         # steady reads prove the minimum is met
        return _out("below_min", ev, achievable, 0.0, "The fixed part (%d tokens) is under the %d-token minimum; "
                    "nothing can cache as it is." % (static_tok, cmin), None)
    if r["explicit"] and reads_share < 0.05:
        return _out("no_mark", ev, achievable, saving, "Mark the fixed block with cache_control; the request text "
                    "doesn't change.", "safe")
    if site["static_after_dynamic"] > 0.5 and fixed_now < cmin:
        return _out("dynamic_first", ev, achievable, saving, "Per-call content comes before the fixed instructions, "
                    "so no two calls share a prefix. Move the fixed part first and the per-call context after it.",
                    "proof")
    if site["tool_sets"] > 1 and site["tool_sets"] > max(3, calls * 0.01) and share_now < OK_SHARE * achievable:
        return _out("tools_vary", ev, achievable, saving, "The tool list changes from call to call and tools lead "
                    "the prefix. Send one stable tool set per call-site.", "proof")
    if ttl and ttl.get("loss", 0) >= MATERIAL * (ledger["cost"] or 0) and ttl["best_ttl"] == 0:
        return _out("loss", ev, achievable, ttl["loss"], "The prompt is written to cache far more often than it is read "
                    "again, and each write costs extra. Not marking it is cheaper by the amount shown.", "safe")
    if ttl and ttl["best_ttl"] not in (0, ttl["default_ttl"]) and ttl["saving"] >= MATERIAL * (ledger["cost"] or 0):
        return _out("gaps", ev, achievable, ttl["saving"], "Calls repeat the prefix after it expired. A %s lifetime "
                    "saves the difference." % _ttl_name(ttl["best_ttl"]), "safe")
    if share_now >= OK_SHARE * achievable:
        return _out("ok", ev, achievable, 0.0, "Reads already cover most of the fixed part.", None)
    if ev["versions"] >= CHURN_VERSIONS:
        return _out("versions_churn", ev, achievable, saving, "The fixed part changed %d times in the window; each "
                    "change restarts the cache." % ev["versions"], None)
    if sparse:
        return _out("sparse", ev, achievable, 0.0, "Most calls arrive after the prefix expired, and %s offers no "
                    "longer lifetime; only more traffic or a shared prefix across call-sites helps." % prov, None)
    return _out("unexplained", ev, achievable, saving, "A constant fixed part repeated often, yet few reads: the "
                "provider or the gateway isn't caching it. Check the rows.", None)


def _ttl_name(secs):
    return "%d-minute" % (secs // 60) if secs < 3600 else "%d-hour" % (secs // 3600)


def _price(model):
    from app.catalog import PRICE, canonical_model
    return PRICE.get(canonical_model(model) or model or "")


def _out(cause, ev, achievable, saving, suggestion, proof):
    return {"cause": cause, "evidence": ev, "achievable_share": round(achievable, 3), "saving": saving,
            "suggestion": suggestion, "proof": proof}
