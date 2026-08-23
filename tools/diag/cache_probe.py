"""PAID (cents) cache write/read probe: does caching actually WRITE then READ on a given model, through whatever
transport llm_client is wired to (direct Anthropic here, a LiteLLM gateway in prod)?

Sends one identical >cache_min prefix N times and dumps the provider's OWN usage counters each call:
  - EXPLICIT providers (Anthropic): cold call WRITES (`cache_creation_input_tokens`), warm calls READ
    (`cache_read_input_tokens`). Both are billed + counted.
  - AUTO providers (OpenAI/Google): NO write fee, no write counter — the only observable is a best-effort warm
    cached read (`prompt_tokens_details.cached_tokens`). A zero there is INCONCLUSIVE, not proof caching is off.

Reads BOTH counter shapes so it works direct or via the gateway, and runs through `llm_client.complete()` on
purpose — so when you swap llm_client for the LiteLLM path in prod, this probe rides that swap unchanged. Creds
come from tools.diag._llm (the same .env keys llm_client reads).

    python -m tools.diag.cache_probe --model claude-haiku-4-5          # ~3 completions, max_tokens=1
    python -m tools.diag.cache_probe --model gem36 --n 4 --append      # a proxy alias, through the gateway
"""
import os
import sys
import json
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from tools.diag._llm import BASE, md_write
from auditor.util import canonical_model, cache_min, cache_mode, tier, provider_of, approx_tokens

_LOREM = ("The auditor proves a prefix caches by reading the provider's own counters rather than estimating. "
          "Caching cannot change the generated output, so the round-trip counters are the entire proof. ")


def _prefix_for(cmin):
    """A deterministic, contiguous prefix that clears the model's cache minimum with margin.

    NB: our approx_tokens (~4 chars/tok) OVERESTIMATES tokens for dense prose (real density ~4.5-5 chars/tok),
    so an estimate that looks >min can tokenize BELOW min and silently not cache. We overshoot at 6 chars/tok
    over a floor of 4096 (the largest provider min) + 2048 margin, so the REAL token count clears the minimum
    for ANY model — even when a proxy alias resolves to an unknown cmin. Extra input tokens are cents."""
    target_chars = (max(cmin, 4096) + 2048) * 6
    reps = target_chars // len(_LOREM) + 1
    return (_LOREM * reps)[:target_chars]


def _counters(u):
    """All cache signals from one usage object, both shapes, plus the raw dump so we can SEE the exact fields."""
    det = getattr(u, "prompt_tokens_details", None)
    cached = getattr(det, "cached_tokens", None) if det is not None else None
    if cached is None and isinstance(det, dict):
        cached = det.get("cached_tokens")
    try:
        raw = u.model_dump()
    except Exception:
        raw = {k: getattr(u, k) for k in dir(u) if not k.startswith("_") and not callable(getattr(u, k))}
    return {"input": int(getattr(u, "input_tokens", 0) or 0),
            "output": int(getattr(u, "output_tokens", 0) or 0),
            "write": int(getattr(u, "cache_creation_input_tokens", 0) or 0),   # Anthropic explicit write
            "read": int(getattr(u, "cache_read_input_tokens", 0) or 0),        # Anthropic explicit read
            "cached_openai": int(cached or 0),                                 # OpenAI/Gemini normalized read
            "raw": raw}


def run(a):
    from app.services import llm_client
    model = a.model
    canon = canonical_model(model) or model
    mode = cache_mode(canon)
    cmin = cache_min(canon)
    prefix = _prefix_for(cmin)
    ptok = approx_tokens(prefix)
    blocks = [llm_client.cache_block(prefix)]   # cache_control breakpoint; llm_client STRIPS it for non-claude
    is_gateway = bool(BASE)

    L = ["# Cache write/read probe — `%s`" % model, "",
         "- canonical: **%s**  ·  provider: **%s**  ·  cache_mode: **%s**  ·  cache_min: **%d** tok"
         % (canon, provider_of(canon) or "?", mode, cmin),
         "- prefix sent: **~%d tok** (>%d min)  ·  transport base_url: `%s`  ·  gateway: **%s**"
         % (ptok, cmin, BASE or "(direct Anthropic)", "yes" if is_gateway else "no (direct)"),
         "- calls: **%d** (max_tokens=1 each; user turn varies so the shared prefix is what can cache)" % a.n, ""]

    rows, err = [], None
    for i in range(a.n):
        t0 = time.time()
        try:
            r = llm_client.complete(model=model, max_tokens=1, system=blocks,
                                    messages=[{"role": "user", "content": "probe turn %d" % i}])
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, str(e)[:400]); break
        c = _counters(r.usage); c["ms"] = round((time.time() - t0) * 1000); rows.append(c)

    if not rows:
        L += ["**Call failed** — nothing to report.", "", "```", err or "(no rows)", "```"]
        return md_write(L, a.out, a.append)

    L += ["| call | input | output | write (anthropic) | read (anthropic) | cached (openai-shape) | ms |",
          "|---|---|---|---|---|---|---|"]
    for i, c in enumerate(rows):
        L.append("| %d%s | %d | %d | %d | %d | %d | %d |" %
                 (i, " (cold)" if i == 0 else " (warm)", c["input"], c["output"], c["write"], c["read"],
                  c["cached_openai"], c["ms"]))
    L += [""]

    warm = rows[1:] or rows
    warm_read = max((c["read"] for c in warm), default=0)
    warm_cached = max((c["cached_openai"] for c in warm), default=0)
    cold_write = rows[0]["write"]
    if mode == "explicit":
        if warm_read > 0:
            verdict = "**WORKS (explicit).** Cold call wrote %d tok to cache; warm call read %d tok back — provider ground truth." % (cold_write, warm_read)
        else:
            verdict = "**NO READ observed.** Sent a cache_control breakpoint but no warm read — check the model min, the gateway, or whether cache_control survived (see raw)."
    else:
        obs = max(warm_read, warm_cached)
        if obs > 0:
            verdict = "**WORKS (auto).** No write fee for this provider; warm call surfaced a cached read of %d tok (field: %s)." % (
                obs, "cache_read_input_tokens" if warm_read else "prompt_tokens_details.cached_tokens")
        else:
            verdict = "**NO HIT — INCONCLUSIVE.** Auto caching is best-effort; a zero here is NOT proof it is off. Retry back-to-back, ensure >min tokens, or confirm the gateway surfaces cached_tokens (see raw)."
    L += ["## Verdict", verdict, "",
          "_write/read framing: only EXPLICIT providers (Anthropic) bill + count a write; AUTO providers "
          "(OpenAI/Google) have no write fee — the only observable is the warm cached read._", ""]

    L += ["## Raw usage (cold call, then first warm call) — to pin the exact gateway field", "", "```json",
          json.dumps(rows[0]["raw"], indent=2, default=str)[:1500]]
    if len(rows) > 1:
        L += ["", json.dumps(rows[1]["raw"], indent=2, default=str)[:1500]]
    L += ["```", ""]
    return md_write(L, a.out, a.append)


def main():
    ap = argparse.ArgumentParser(description="Cache write/read round-trip through llm_client (paid, cents).")
    ap.add_argument("--model", required=True, help="catalog name or a proxy alias (e.g. claude-haiku-4-5, gem36)")
    ap.add_argument("--n", type=int, default=3, help="calls to send (1 cold + warms)")
    ap.add_argument("--out", default="cache_probe.md")
    ap.add_argument("--append", action="store_true")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
