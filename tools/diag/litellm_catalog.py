"""READ-ONLY ($0) LiteLLM deployment debug: what does THIS gateway serve, and how does it map onto models.json?

Hits every list-ish endpoint a LiteLLM proxy MIGHT expose in ONE run, reports each one's status, and DUMPS the raw
JSON of every endpoint that answers — so a single prod test reveals which endpoints exist (auth/version differ) and
what each returns, with no second round-trip. Then builds the three-way join that makes cost math correct + the
downgrade lever actionable:

    trace model string -> proxy deployment (model_name -> litellm_params.model) -> models.json entry

and the AVAILABILITY GATE: a catalog model this gateway does NOT serve is not a valid downgrade target here.

`--mock` runs the same join against a built-in representative deployment so the report can be shaped with no proxy
in reach. Creds/base_url come from tools.diag._llm (the same .env keys llm_client reads). No writes to models.json.

    python -m tools.diag.litellm_catalog --mock                 # segregate-and-use shape, $0, no proxy
    python -m tools.diag.litellm_catalog --out lp.md            # live: probe all endpoints + dump raw JSON
"""
import os
import sys
import json
import argparse
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from tools.diag._llm import BASE, KEY, md_write, resolve_catalog
from auditor import util
from auditor.util import cache_min, cache_mode, tier, PRICE


def _roots():
    """Candidate proxy roots to try: base_url as given, and with a trailing /anthropic or /v1 stripped (llm_client
    may point the Anthropic SDK at a sub-path, but /model/info lives at the root)."""
    if not BASE:
        return []
    b = BASE.rstrip("/")
    cands = [b]
    for suf in ("/anthropic", "/v1"):
        if b.endswith(suf):
            cands.append(b[: -len(suf)])
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c); out.append(c)
    return out


def _get_json(url):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % (KEY or ""),
                                               "x-api-key": KEY or "", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


# every list-ish endpoint a LiteLLM proxy MIGHT expose — probed together so one run maps the deployment's surface.
_ENDPOINTS = ("/model/info", "/v1/model/info", "/model_group/info", "/v1/models", "/models")


def _probe_all():
    """One record per attempt: {endpoint,url,status,count,raw,err}. status = HTTP code or None. Never raises."""
    out = []
    for root in _roots():
        for ep in _ENDPOINTS:
            url = root + ep
            rec = {"endpoint": ep, "url": url, "status": None, "count": 0, "raw": None, "err": None}
            try:
                raw = _get_json(url)
                rec["status"] = 200
                rec["raw"] = raw
                data = raw.get("data") if isinstance(raw, dict) else raw
                rec["count"] = len(data) if isinstance(data, list) else (1 if data else 0)
            except urllib.error.HTTPError as e:
                rec["status"] = e.code
                rec["err"] = str(e)[:200]
            except Exception as e:
                rec["err"] = "%s: %s" % (type(e).__name__, str(e)[:200])
            out.append(rec)
    return out


def _normalize(rec):
    """A 200 endpoint record -> ([{model_name, underlying, model_info}], rich?). 'rich' means the underlying
    provider/model came through (the alias->catalog join is possible); ids-only sources degrade it."""
    raw = rec["raw"]
    data = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(data, list) or not data:
        return ([], False)
    first = data[0]
    if isinstance(first, dict) and ("litellm_params" in first or "model_info" in first):    # /model/info shape
        return ([{"model_name": m.get("model_name"),
                  "underlying": (m.get("litellm_params") or {}).get("model") or m.get("model_name"),
                  "model_info": m.get("model_info") or {}} for m in data], True)
    return ([{"model_name": (m.get("id") if isinstance(m, dict) else m),                    # /v1/models: ids only
              "underlying": (m.get("id") if isinstance(m, dict) else m), "model_info": {}} for m in data], False)


def _dump_raw(rec, out):
    """Save one endpoint's raw JSON next to the report, so we can analyze the real shape offline. Returns filename."""
    tag = rec["endpoint"].strip("/").replace("/", "_")
    fn = os.path.join(os.path.dirname(os.path.abspath(out)) or ".", "capture_%s.json" % tag)
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(rec["raw"], f, indent=2, default=str)
    return os.path.basename(fn)


# a representative /model/info-shaped deployment: every join case in ~one screen (exact, alias, dated, openai,
# open-weight uncatalogued, embedding non-chat, a 'future' catalog id that is nonetheless a real deployment).
_MOCK = [
    {"model_name": "claude-opus-4-8", "underlying": "anthropic/claude-opus-4-8", "model_info": {"mode": "chat"}},
    {"model_name": "smart",           "underlying": "anthropic/claude-sonnet-4-5-20250929", "model_info": {"mode": "chat"}},
    {"model_name": "fast-model",      "underlying": "gemini/gemini-2.5-flash", "model_info": {"mode": "chat"}},
    {"model_name": "gem36",           "underlying": "gemini/gemini-3.6-flash", "model_info": {"mode": "chat"}},
    {"model_name": "cheap",           "underlying": "openai/gpt-5-mini", "model_info": {"mode": "chat"}},
    {"model_name": "llama-fast",      "underlying": "groq/llama-3.3-70b-versatile", "model_info": {"mode": "chat"}},
    {"model_name": "ds",             "underlying": "deepseek/deepseek-chat", "model_info": {"mode": "chat"}},
    {"model_name": "embed",           "underlying": "openai/text-embedding-3-small", "model_info": {"mode": "embedding"}},
]


def run(a):
    L = ["# LiteLLM deployment vs models.json — segregate & use", ""]

    if a.mock:
        deps, rich, src = _MOCK, True, "MOCK (built-in representative deployment)"
    else:
        if not BASE:
            L += ["**No proxy configured.** base_url unset (ANTHROPIC_BASE_URL / LITELLM_BASE_URL). This box talks "
                  "direct Anthropic, so there is no deployment list to fetch. Run with `--mock` for the shape, "
                  "or set the gateway base_url + key in .env and re-run.", ""]
            return md_write(L, a.out, a.append)
        probes = _probe_all()
        L += ["Probed base_url: `%s`  ·  key: %s" % (BASE, "set" if KEY else "MISSING"), "",
              "## Endpoints probed (single run)", "",
              "| endpoint | url | status | items | raw dump |", "|---|---|---|---|---|"]
        dumps = []
        for r in probes:
            fn = _dump_raw(r, a.out) if r["status"] == 200 and r["raw"] is not None else "—"
            if fn != "—":
                dumps.append(fn)
            L.append("| `%s` | `%s` | %s | %d | %s |" % (
                r["endpoint"], r["url"], r["status"] if r["status"] is not None else ("ERR: " + (r["err"] or "?")),
                r["count"], fn))
        L += [""]
        if dumps:
            L += ["Raw responses saved: %s (share these back)." % ", ".join("`%s`" % d for d in sorted(set(dumps))), ""]
        best = None
        for r in probes:                                        # prefer a RICH (/model/info) source
            if r["status"] == 200:
                d, rich = _normalize(r)
                if d and rich:
                    best = (d, True, r["url"]); break
        if best is None:                                        # else any ids-only 200 with data
            for r in probes:
                if r["status"] == 200:
                    d, rich = _normalize(r)
                    if d:
                        best = (d, False, r["url"]); break
        if best is None:
            L += ["**No endpoint returned a usable model list.** See statuses above — likely auth (401/403) or a "
                  "different proxy version. Share the raw dumps and I'll adapt the parser.", ""]
            return md_write(L, a.out, a.append)
        deps, rich, src = best

    if not rich:
        L += ["> ⚠ **ids-only source** (`%s`): the underlying provider/model isn't exposed, so the alias→catalog "
              "join is degraded — only aliases that literally match a catalog name resolve. `/model/info` (with an "
              "admin key) is what enables the full join." % src, ""]
    L += ["Source: **%s**   ·   deployed entries: **%d**" % (src, len(deps)), ""]
    L += ["| deployed name (alias) | underlying model | mode | catalog match | tier | our in/out $ | cache_mode | cache_min | status |",
          "|---|---|---|---|---|---|---|---|---|"]
    matched, uncat, nonchat = [], [], []
    for m in deps:
        mode = (m["model_info"] or {}).get("mode", "chat")
        cat = resolve_catalog(m["underlying"])
        if mode != "chat":
            status = "NON-CHAT (out of downgrade/cache scope)"; nonchat.append(m)
        elif cat:
            status = "MATCHED"; matched.append(cat)
        else:
            status = "**UNCATALOGUED — needs details**"; uncat.append(m)
        p = PRICE.get(cat, {})
        io = ("%s/%s" % (p.get("input"), p.get("output"))) if cat else "—"
        L.append("| `%s` | `%s` | %s | %s | %s | %s | %s | %s | %s |" % (
            m["model_name"], m["underlying"], mode, cat or "—", tier(cat) or "—", io,
            cache_mode(cat) if cat else "—", cache_min(cat) if cat else "—", status))
    L += [""]

    deployed_chat = sum(1 for m in deps if (m["model_info"] or {}).get("mode", "chat") == "chat")
    L += ["## Coverage",
          "- deployed chat models: **%d**  ·  matched to catalog: **%d**  ·  uncatalogued: **%d**  ·  non-chat: **%d**"
          % (deployed_chat, len(set(matched)), len(uncat), len(nonchat)), ""]
    if uncat:
        L += ["**Available here but NOT in our catalog** (price/tier/cache unknown — add before we can audit them):"]
        L += ["- `%s`  (deployed as `%s`)" % (m["underlying"], m["model_name"]) for m in uncat] + [""]

    callable_cat = [n for n in PRICE if util.is_callable(n)]
    not_deployed = [n for n in callable_cat if n not in set(matched)]
    L += ["## Availability gate for the downgrade lever",
          "In our catalog + callable, but **NOT served by this gateway** -> not a valid downgrade target here "
          "(next_cheaper must be gated on the deployed set):", "",
          ", ".join("`%s`" % n for n in sorted(not_deployed)) or "_all catalog models are deployed_", ""]
    return md_write(L, a.out, a.append)


def main():
    ap = argparse.ArgumentParser(description="LiteLLM deployment debug: deployed models vs models.json ($0).")
    ap.add_argument("--mock", action="store_true", help="use the built-in representative deployment (no proxy needed)")
    ap.add_argument("--out", default="litellm_catalog.md")
    ap.add_argument("--append", action="store_true")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
