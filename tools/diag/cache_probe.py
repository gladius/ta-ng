"""PAID (cents) cache DIAGNOSTIC: does caching write/read on a given model, and WHERE does the gateway report it?

The point of this tool is to surface what we DON'T know — so it never cherry-picks a usage field. Each call it
captures the FULL response, recursively finds EVERY key whose name contains "cache" (at any depth), and lists all
usage keys — so if the count lives under an unexpected name (cachedContentTokenCount / cached_tokens /
total_cached_tokens / cache_read_input_tokens / prompt_tokens_details.*), we SEE it instead of missing it.

It runs the identical >cache_min round-trip in BOTH response formats, side by side:
  - anthropic  : through llm_client (the app's transport) -> the gateway's /v1/messages. Usage is Anthropic-shaped
                 (input_tokens / cache_read_input_tokens) — it has NO prompt_tokens_details, which is why Google's
                 normalized cached_tokens can be invisible here.
  - openai     : a raw POST to the gateway's /v1/chat/completions. Usage is OpenAI-shaped
                 (prompt_tokens / prompt_tokens_details.cached_tokens) — where LiteLLM puts Google's cache count.

Google (Vertex + Gemini API) implicit caching is DEFAULT-ON for 2.5+/3.x (min 2048 / 4096 tok), so no explicit
setup is needed to observe a hit — only the right response shape. Creds/base_url come from tools.diag._llm.

    python -m tools.diag.cache_probe --model gem36                    # both formats (default), 3 calls each
    python -m tools.diag.cache_probe --model gem36 --format openai --n 4
"""
import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from tools.diag._llm import BASE, KEY, md_write
from auditor.util import canonical_model, cache_min, cache_mode, tier, provider_of, approx_tokens

_LOREM = ("The auditor proves a prefix caches by reading the provider's own counters rather than estimating. "
          "Caching cannot change the generated output, so the round-trip counters are the entire proof. ")


def _prefix_for(cmin):
    """A deterministic, contiguous prefix that clears the cache minimum with margin (real density ~4.5-5 chars/tok;
    our approx_tokens ~4 OVERESTIMATES, so we overshoot at 6 chars/tok over max(cmin,4096)+2048 to be safe)."""
    target_chars = (max(cmin, 4096) + 2048) * 6
    reps = target_chars // len(_LOREM) + 1
    return (_LOREM * reps)[:target_chars]


# ── the heart: never guess a field — scan the WHOLE response for anything cache-ish ──────────────────────────
def _walk_cache_keys(obj, path=""):
    """Yield (dotted_path, value) for every dict key whose name contains 'cache' (case-insensitive), at any depth.
    Catches cache_read_input_tokens, cache_creation*, cached_tokens, cachedContentTokenCount, total_cached_tokens…"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = "%s.%s" % (path, k) if path else str(k)
            if "cache" in str(k).lower():
                yield (p, v if not isinstance(v, (dict, list)) else json.dumps(v))
            yield from _walk_cache_keys(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_cache_keys(v, "%s[%d]" % (path, i))


def _all_keys(obj, path="", out=None):
    """Every leaf key path in a dict tree — so we see the whole usage shape, not just the cache-ish parts."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = "%s.%s" % (path, k) if path else str(k)
            if isinstance(v, (dict, list)):
                _all_keys(v, p, out)
            else:
                out.append(p)
    elif isinstance(obj, list) and obj:
        _all_keys(obj[0], "%s[0]" % path, out)
    return out


def _tok(usage, *names):
    for n in names:
        v = usage.get(n)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


# ── transport A: anthropic /v1/messages via the app's own client ─────────────────────────────────────────────
def _anthropic_call(model, prefix, i):
    from app.services import llm_client
    blocks = [llm_client.cache_block(prefix)]                 # cache_control breakpoint; stripped for non-claude
    r = llm_client.complete(model=model, max_tokens=1, system=blocks,
                            messages=[{"role": "user", "content": "probe turn %d" % i}])
    try:
        full = r.model_dump()
    except Exception:
        full = {"usage": {k: getattr(r.usage, k, None) for k in dir(r.usage) if not k.startswith("_")}}
    return full, (full.get("usage") or {})


# ── transport B: openai /v1/chat/completions via a raw POST (llm_client can't emit this shape) ────────────────
def _roots():
    if not BASE:
        return []
    b = BASE.rstrip("/")
    cands = [b] + [b[:-len(s)] for s in ("/anthropic", "/v1") if b.endswith(s)]
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c); out.append(c)
    return out


def _openai_endpoint():
    """Discover a working chat-completions URL once (POST-probe with a trivial body)."""
    body = json.dumps({"model": "___probe___", "messages": [{"role": "user", "content": "x"}], "max_tokens": 1}).encode()
    for root in _roots():
        for path in ("/v1/chat/completions", "/chat/completions"):
            url = root + path
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Authorization": "Bearer %s" % (KEY or ""), "x-api-key": KEY or "",
                                                  "Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=20)
                return url                                    # 2xx (unlikely for a bogus model) — endpoint exists
            except urllib.error.HTTPError as e:
                if e.code in (400, 404, 422):                 # 400/422 = endpoint exists, model bogus -> it's live
                    if e.code != 404:
                        return url
                    continue
                return url                                    # 401/403 etc. -> endpoint exists, auth aside
            except Exception:
                continue
    return None


def _openai_call(url, model, prefix, i):
    body = json.dumps({"model": model, "max_tokens": 1,
                       "messages": [{"role": "system", "content": prefix},
                                    {"role": "user", "content": "probe turn %d" % i}]}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": "Bearer %s" % (KEY or ""), "x-api-key": KEY or "",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        full = json.loads(r.read())
    return full, (full.get("usage") or {})


# ── run one format, dump everything ──────────────────────────────────────────────────────────────────────────
def _run_format(fmt, model, prefix, n, L):
    L += ["## Format: %s" % fmt, ""]
    rows, err = [], None
    oa_url = None
    if fmt == "openai":
        oa_url = _openai_endpoint()
        if not oa_url:
            L += ["**No /chat/completions endpoint reachable** on `%s` — skipped. (Only the Anthropic /v1/messages "
                  "path may be exposed, or a different chat path.)" % (BASE or "(no base_url)"), ""]
            return
        L += ["endpoint: `%s`" % oa_url, ""]
    for i in range(n):
        t0 = time.time()
        try:
            full, usage = (_anthropic_call(model, prefix, i) if fmt == "anthropic"
                           else _openai_call(oa_url, model, prefix, i))
        except urllib.error.HTTPError as e:
            err = "HTTP %s: %s" % (e.code, (e.read()[:400].decode("utf-8", "replace") if hasattr(e, "read") else e))
            break
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, str(e)[:400]); break
        rows.append({"full": full, "usage": usage, "ms": round((time.time() - t0) * 1000),
                     "in": _tok(usage, "input_tokens", "prompt_tokens"),
                     "out": _tok(usage, "output_tokens", "completion_tokens"),
                     "cache_keys": list(_walk_cache_keys(full))})
    if not rows:
        L += ["**Call failed** — nothing to report.", "", "```", err or "(no rows)", "```", ""]
        return

    L += ["| call | in | out | cache-ish keys found (path = value) | ms |", "|---|---|---|---|---|"]
    for i, r in enumerate(rows):
        ck = "; ".join("`%s`=%s" % (p, v) for p, v in r["cache_keys"]) or "— none —"
        L.append("| %d%s | %d | %d | %s | %d |" % (i, " (cold)" if i == 0 else " (warm)", r["in"], r["out"], ck, r["ms"]))
    L += [""]

    seen_keys = sorted(set(k for r in rows for k in _all_keys(r["usage"])))
    L += ["**All usage keys seen this format:** %s" % (", ".join("`%s`" % k for k in seen_keys) or "(none)"), ""]

    warm_hits = [float(v) for r in rows[1:] for p, v in r["cache_keys"]
                 if isinstance(v, (int, float)) or (isinstance(v, str) and v.replace(".", "", 1).isdigit())]
    warm_hits = [v for v in warm_hits if v]
    if warm_hits:
        L += ["**Verdict (%s): CACHE READ OBSERVED** — a warm call reported a non-zero cache field (max %g). "
              "This is the shape that surfaces it." % (fmt, max(warm_hits)), ""]
    else:
        anyc = any(r["cache_keys"] for r in rows)
        L += ["**Verdict (%s): no non-zero cache field on a warm call.** %s" %
              (fmt, "Cache keys exist but read 0." if anyc else "NO cache-ish key present at all in this response shape."),
              ""]

    L += ["<details><summary>raw response — cold call</summary>", "", "```json",
          json.dumps(rows[0]["full"], indent=2, default=str)[:2500], "```", "</details>", ""]
    if len(rows) > 1:
        L += ["<details><summary>raw response — first warm call</summary>", "", "```json",
              json.dumps(rows[1]["full"], indent=2, default=str)[:2500], "```", "</details>", ""]


def run(a):
    model = a.model
    canon = canonical_model(model) or model
    cmin = cache_min(canon)
    prefix = _prefix_for(cmin)
    L = ["# Cache diagnostic — `%s`" % model, "",
         "- canonical: **%s**  ·  provider: **%s**  ·  cache_mode(catalog): **%s**  ·  cache_min: **%d** tok"
         % (canon, provider_of(canon) or "? (uncatalogued)", cache_mode(canon), cmin),
         "- prefix sent: **~%d tok**  ·  gateway base_url: `%s`  ·  calls/format: **%d**"
         % (approx_tokens(prefix), BASE or "(direct Anthropic)", a.n),
         "- method: identical prefix repeated; user turn varies so the shared prefix is what can cache. We DUMP the "
         "full response and scan every key for 'cache' — no field is assumed.", ""]

    fmts = ["anthropic", "openai"] if a.format == "both" else [a.format]
    for fmt in fmts:
        _run_format(fmt, model, prefix, a.n, L)

    L += ["## How to read this",
          "- If a cache field appears **only** under `openai` (e.g. `prompt_tokens_details.cached_tokens`), the "
          "Anthropic transport is hiding Google's count — the fix is which response shape we read, not caching.",
          "- If **neither** format shows a non-zero warm cache read, either implicit caching didn't fire (best-effort) "
          "or the gateway doesn't forward the count — compare the raw dumps.", ""]
    md_write(L, a.out, a.append)


def main():
    ap = argparse.ArgumentParser(description="Cache diagnostic: dump full response + scan all cache keys, both formats.")
    ap.add_argument("--model", required=True, help="catalog name or a proxy alias (e.g. gem36, gemini-3.7-flash)")
    ap.add_argument("--format", choices=("anthropic", "openai", "both"), default="both")
    ap.add_argument("--n", type=int, default=3, help="calls per format (1 cold + warms)")
    ap.add_argument("--out", default="cache_probe.md")
    ap.add_argument("--append", action="store_true")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
