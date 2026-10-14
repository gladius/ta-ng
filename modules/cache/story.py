"""What a call-site's numbers mean, in plain sentences: what it is, what happens now, what to do and what it's
worth. The pages show these sentences; the charts are evidence underneath."""
from modules.cache.ttl import _price  # noqa: F401  (keeps the price lookup in one place)

_TTL = lambda s: "%d-minute" % (s // 60) if s < 3600 else "%d-hour" % (s // 3600) if s < 86400 else "%d-day" % (s // 86400)


def _usd(v):
    return "$%.2f" % v if v >= 1 else "$%.2f" % v


def tell(site, window):
    """site: a report site entry (models with diagnosis and ttl, profile, total) -> {now, why, action, worth}."""
    tot = site["total"]
    calls = tot["calls"]
    share = tot["cached_tokens"] / tot["prompt_tokens"] if tot["prompt_tokens"] else 0.0
    main = max(site["models"].values(), key=lambda m: m["calls"])
    d = main["diagnosis"] or {}
    ev = d.get("evidence", {})
    prof = site.get("profile") or {}
    per_call = prof.get("per_call") or "the per-call content"
    cause = d.get("cause")
    models = sorted(site["models"], key=lambda m: -site["models"][m]["calls"])
    sent = "sent to %s" % models[0] if len(models) == 1 else "sent to %s (mostly %s)" % (", ".join(models), models[0])
    now = "%s calls in %d days, %s. %d%% of each prompt is reused from cache" % (
        "{:,}".format(calls), window, sent, round(100 * share))
    now += " (nothing is)." if share < 0.01 else "."
    worth = site.get("saving") if site.get("saving") is not None else (d.get("saving") or 0)   # all models of the site
    why, action = "", ""
    if cause == "ok":
        why = "The part that stays the same every call is being reused."
        action = "Nothing to do."
    elif cause == "no_mark":
        why = "The request doesn't mark its fixed part, and Claude only caches a marked prefix."
        action = "Add the cache mark on the fixed part. No text changes."
    elif cause == "below_min":
        why = "The part that stays the same is %s tokens, under the %s-token minimum %s caches." % (
            "{:,}".format(ev.get("fixed_tokens", 0)), "{:,}".format(ev.get("cache_min", 0)), ev.get("provider", "the provider"))
        action = "Nothing can be reused as it is. A larger shared prefix (merging call-sites, or moving shared instructions into one) would be needed."
    elif cause == "dynamic_first":
        why = "%s comes before the fixed instructions, so the start of every call is different and nothing is shared%s." % (
            per_call[0].upper() + per_call[1:], " except on follow-up turns of the same conversation" if share > 0.05 else "")
        action = "Move the fixed instructions first and the per-call content after them: ~%s tokens would then be reused on every call. Changing the prompt can change answers, so this is proven on the same model before anyone applies it." % "{:,}".format(ev.get("fixed_tokens", 0))
    elif cause == "tools_vary":
        why = "The tool list changes from call to call (%d different sets) and tools come first in what the cache sees." % ev.get("tool_sets", 0)
        action = "Send one stable tool set for this call-site; put the varying part in the message instead."
    elif cause == "loss":
        t = main.get("ttl") or {}
        why = "The prompt is written to cache much more often than it is read back, and every write costs extra."
        action = "Stop marking this prompt for cache: it saves about $%.2f over the window. No text changes." % t.get("loss", 0)
    elif cause == "gaps":
        t = main.get("ttl") or {}
        why = "Most calls come after the cache expired (%s)." % _TTL(t.get("default_ttl", 300))
        action = "Choose the %s lifetime: %s more calls would reuse the prompt. No text changes." % (
            _TTL(t.get("best_ttl", 3600)), "{:,}".format((t.get("options", {}).get(t.get("best_ttl"), {}).get("reads", 0)
                                                           - t.get("options", {}).get(t.get("default_ttl"), {}).get("reads", 0))))
    elif cause == "sparse":
        why = "Calls are too far apart for %s's cache lifetime, and that lifetime can't be extended there." % ev.get("provider", "the provider")
        action = "Nothing to set. Only more traffic, or a prefix shared with other call-sites, would help."
    elif cause == "versions_churn":
        why = "The prompt changed %d times in the window; every change restarts the cache." % ev.get("versions", 0)
        action = "Change the fixed part less often, or keep the changing text after the fixed part."
    elif cause == "unexplained":
        why = "The prompt is the same every call and repeated often, yet little is reused."
        action = "The provider or the gateway isn't caching it: check the raw rows for this call-site."
    return {"now": now, "why": why, "action": action, "worth": worth, "share": round(share, 3),
            "proof": d.get("proof"), "cause": cause}
