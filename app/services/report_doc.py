"""Report export — a downloadable ZIP of exactly TWO markdown files, built ONLY from the frozen proof (no LLM in the
loop, so the deliverable is reproducible):

  report.md    the APPLY GUIDE — one block per proven fix (what / where / how / why / verify / caveat, artifacts
               INLINE and paste-ready) + a machine-readable index + the honest rejections. Self-contained *for
               action*: a human or a coding assistant can apply every fix from this file alone.
  evidence.md  the PROOF APPENDIX — the raw runs behind every verdict: per call-site, the reference set (the current
               model's own outputs) vs the re-runs, each tagged, with the rate. Read this to *verify* a
               recommendation; you never need it to apply one.

The split is deliberate: applying a fix needs the guide, not the receipts (evidence is pure noise to an agent
applying a change, and balloons for a large agent); verifying a fix needs the receipts, on demand. stdlib zipfile
only — zero dependencies.
"""
import io
import json
import re
import zipfile

# per-INPUT rollup verdict, shown with the system's own words — BORDERLINE is its own state, NOT "not safe".
_IV = {"SAFE": "safe", "NOT-SAFE": "not safe", "BORDERLINE": "borderline", "LOW-EVIDENCE": "low evidence"}

_LEVER_LABEL = {"downgrade": "Model downgrade", "cache": "Cache-prefix expansion", "compress": "Prompt compression"}
# the SAME short label in report.md's cross-reference and evidence.md's heading, so a reader matches them by name.
_EV_LABEL = {"downgrade": "model downgrade", "cache": "cache-prefix", "compress": "prompt compression"}


def _fmt(n):
    return "{:,.0f}".format(n or 0)


def _sys_first_line(detail):
    """A greppable fingerprint of the call-site's system prompt — its first non-blank line — so a human or coding
    assistant can locate the exact prompt in their source. From the compress lever's `original`, else the first
    input's captured system prompt (`system` on the refset engine, `system_before` on the legacy engine)."""
    sysm = detail.get("original") or ""
    if not sysm:
        inps = detail.get("inputs") or []
        first = inps[0] if inps else {}
        sysm = first.get("system") or first.get("system_before") or ""
    return next((ln.strip() for ln in sysm.splitlines() if ln.strip()), "")[:140]


def _drift(detail):
    """Why a lever was rejected: the first NOT-SAFE input's judge reason, else the finding's own reason."""
    for r in detail.get("inputs") or []:
        if r.get("verdict") == "NOT-SAFE":
            return r.get("note") or r.get("reason") or ""
    return detail.get("reason") or ""


def _rate(inp):
    """The per-input rate line. Refset downgrade reads the cheaper's fit against the reference set alongside the
    original's own self-consistency; the legacy cache/compress engine reads kept vs an optional baseline."""
    if inp.get("self_rate"):                                   # refset downgrade: cheaper fit · original self-consistency
        return "cheaper %d/%d · original %s" % (inp.get("kept", 0), inp.get("k", 0), inp["self_rate"])
    r = "kept %d/%d" % (inp.get("kept", 0), inp.get("k", 0))
    if inp.get("self_kept") is not None:                       # legacy engine baseline
        r += " · baseline %d/%d" % (inp["self_kept"], inp.get("k", 0))
    return r


# ─────────────────────────────────────────── report.md — the apply guide ───────────────────────────────────────────

def _report_md(f, proof):
    """The deliverable: an APPLY GUIDE across all three levers, built ONLY from the frozen proof. One block per
    recommended fix (what / where / how / why / verify / caveat, artifacts INLINE and paste-ready), a machine-readable
    index for a coding assistant, and an honest list of what was tested and rejected. The raw runs live in
    evidence.md; nothing here is invented — every figure is measured."""
    calls = f["calls"]
    unit = "%s calls" % _fmt(calls)
    results = proof["results"] if proof else []
    total = (proof.get("total", 0) if proof else 0)
    dg_total = (proof.get("downgrade_usd", 0) if proof else 0)
    ca_total = (proof.get("cache_usd", 0) if proof else 0)
    co_total = (proof.get("compress_usd", 0) if proof else 0)

    # ONE pass over the proof: split every lever result into a RECOMMENDED fix or an honest REJECTION.
    fixes, rejects = [], []                     # fixes: (lever, x, detail, usd)   rejects: (call_site, lever, verdict, reason)
    for x in results:
        key = x.get("key") or x["node"]
        d = x.get("downgrade")
        if d and d.get("verdict") == "SAFE":
            fixes.append(("downgrade", x, d, x.get("downgrade_usd", 0)))
        elif d:
            rejects.append((key, "model downgrade", d.get("verdict", "?"), _drift(d)))
        c = x.get("cache")
        if c and not c.get("informational"):
            if c.get("recommend"):
                fixes.append(("cache", x, c, x.get("cache_usd", 0)))
            else:
                rejects.append((key, "cache-prefix", c.get("verdict", "?"), _drift(c)))
        cp = x.get("compress")
        if cp and cp.get("verdict") == "SAFE":
            fixes.append(("compress", x, cp, x.get("compress_usd", 0)))
        elif cp:
            rejects.append((key, "compression", cp.get("verdict", "?"), _drift(cp)))

    L = ["# Token Audit — %s · Apply Guide" % f["agent"], ""]
    L.append("> These are **verified, behaviour-preserving** changes to your agent, each proven on real recorded "
             "inputs. Apply each at the named call-site **exactly as written** — the prompts and prefixes below were "
             "proven verbatim, so editing a proven artifact voids its proof. Advisory & read-only: you decide what to "
             "apply. The raw runs behind every verdict are in **`evidence.md`** — you need only this file to apply.")
    L.append("")
    if total > 0:
        L.append("**$%s in verified savings per %s** — ↓ $%s model downgrade · ⚡ $%s cache-prefix · − $%s compression."
                 % (_fmt(total), unit, _fmt(dg_total), _fmt(ca_total), _fmt(co_total)))
    else:
        L.append("**No safe savings on the audited call-sites** — each re-ran live and needs its current setup. A "
                 "verified result, not an empty one; see *Tested and not recommended* below.")
    L.append("")
    L.append("Audited **%d call-site(s)** across **%s traces**; **%d fix(es) to apply**. All $ per %s."
             % (len(results), f.get("traces", 0), len(fixes), unit))
    L.append("")

    # ── machine-readable index — a coding assistant can parse THIS instead of the prose ──
    idx = []
    for n, (lever, x, det, usd) in enumerate(fixes, 1):
        e = {"fix": n, "lever": lever, "call_site": (x.get("key") or x["node"]), "node": x["node"],
             "graph_path": x.get("graph_path", ""), "saving": round(usd, 2), "basis": unit}
        if lever == "downgrade":
            e["from_model"], e["to_model"] = det.get("model"), det.get("cheaper")
        elif lever == "compress":
            e["from_tokens"], e["to_tokens"] = det.get("before_tok"), det.get("after_tok")
        elif lever == "cache":
            e["cached_tokens_before"] = (det.get("before") or {}).get("read", 0)
            e["cached_tokens_after"] = (det.get("after") or {}).get("read", 0)
        idx.append(e)
    L += ["## Fixes — machine-readable index", "```json", json.dumps(idx, indent=2), "```", ""]

    # ── one APPLY BLOCK per recommended fix — what / where / how / why / verify / caveat, artifacts inline ──
    if fixes:
        L += ["## Fixes to apply", ""]
    for n, (lever, x, det, usd) in enumerate(fixes, 1):
        key = x.get("key") or x["node"]
        L.append("### Fix %d · %s · `%s` — saves $%s per %s" % (n, _LEVER_LABEL[lever], key, _fmt(usd), unit))
        L.append("")
        L.append("**Where**")
        L.append("- Call-site: node `%s`%s, agent `%s`, currently on model `%s`." % (
            x["node"], (" (subgraph `%s`)" % x["graph_path"]) if x.get("graph_path") else "", f["agent"], x["model"]))
        fp = _sys_first_line(det)
        if fp:
            L += ["- Find it by the node name, or by its system prompt which begins:", "  > %s" % fp]
        L += ["", "**Change**"]
        if lever == "downgrade":
            L += ["- Switch this call-site's model:  `%s`  →  `%s`" % (det.get("model"), det.get("cheaper")),
                  "- Nothing else changes — same prompt, same tools."]
        elif lever == "compress":
            L.append("- Replace this call-site's **system prompt** with the version below (paste verbatim). Tokens: "
                     "**%s → %s** (%s). Keep every per-call value exactly as-is."
                     % (det.get("before_tok"), det.get("after_tok"), det.get("mode", "-")))
            L += ["", "```text", det.get("compressed", ""), "```"]
        elif lever == "cache":
            b = (det.get("before") or {}).get("read", 0)
            a = (det.get("after") or {}).get("read", 0)
            L += ["- Make this exact block a **contiguous cached system prefix**: hoist it to the front, set the "
                  "provider's cache breakpoint at its end, and keep every per-call value **after** it, verbatim.",
                  "- Cached tokens per call: **%s → %s** (recovered %s)." % (b, a, det.get("recovered_tok", 0)),
                  "", "```text", det.get("prefix", ""), "```"]
        ni, si, k = det.get("n", 0), det.get("safe_inputs", 0), det.get("k", 0)
        L += ["", "**Why it's safe**"]
        if lever == "downgrade":
            L.append("- Re-ran %d distinct real input(s). On each, we ran your current model %d times to capture its "
                     "own outputs — the **reference set**, i.e. the behaviour and the natural variation it already "
                     "shows — then ran `%s` and judged each of its outputs against that set. It stayed within the set "
                     "on **%d/%d**. The cheaper model is only held to the latitude your current model itself takes, so "
                     "it is never penalised for variation the original already shows." % (ni, k, det.get("cheaper"), si, ni))
        elif lever == "compress":
            L.append("- The compressor kept every rule, number, code and tool name verbatim; behaviour then held on "
                     "**%d/%d** distinct real inputs × %d re-runs, judged against your recorded outputs." % (si, ni, k))
        elif lever == "cache":
            L.append("- Behaviour held on **%d/%d** distinct real inputs × %d re-runs (reordered prompt vs recorded "
                     "output), **and** a live provider round-trip confirmed the new prefix actually cached." % (si, ni, k))
        L.append("- Saves **$%s per %s**." % (_fmt(usd), unit))
        L += ["", "**Verify & caveats**",
              "- After applying, re-audit this call-site (or re-run these inputs and diff behaviour). Full proof: "
              "**`evidence.md`** → `%s` (%s)." % (key, _EV_LABEL[lever])]
        if lever == "compress":
            L.append("- Do **not** further shorten this text — it was proven exactly as written; more cuts void the proof.")
        if lever == "cache":
            L.append("- Savings assume calls arrive within the provider's cache window; a call-site called less often "
                     "than that window won't benefit.")
        L += ["", "---", ""]

    # ── composition: a call-site with more than one fix ──
    seen, multi = set(), []
    for _, x, _, _ in fixes:
        k = x.get("key") or x["node"]
        (multi.append(k) if k in seen else seen.add(k))
    if multi:
        L += ["## Call-sites with more than one fix",
              "Independent — apply both. (Downgrade changes the model; compression/cache change the prompt.) Order "
              "doesn't matter."] + ["- `%s`" % k for k in dict.fromkeys(multi)] + [""]

    # ── the honest 'no' — tested and rejected, so nothing is silently touched ──
    if rejects:
        L += ["## Tested and not recommended (keep current)",
              "We tried these and the proof said no — listed so you know what was tested and *not* to touch. The runs "
              "behind each are in `evidence.md`:"]
        for key, lever, verdict, reason in rejects:
            L.append("- `%s` · %s → **%s**%s" % (key, lever, verdict, (" — %s" % reason) if reason else ""))
        L.append("")

    L.append("---")
    L.append("_Figures per %s — a fixed basis, not a monthly total; multiply by your real call volume to scale. Built "
             "only from the frozen proof: measured live against your recorded traffic, nothing invented._" % unit)
    return "\n".join(L) + "\n"


# ────────────────────────────────────────── evidence.md — the proof appendix ───────────────────────────────────────

def _first_system(inputs):
    for inp in inputs:
        s = inp.get("system") or inp.get("system_before")
        if s:
            return s
    return ""


def _ev_headline(lever, det, x):
    """The one-line summary under a call-site×lever heading in evidence.md."""
    if lever == "downgrade":
        return "current `%s` → `%s` · verdict **%s** (%d/%d input(s) safe)" % (
            det.get("model") or x.get("model"), det.get("cheaper"), det.get("verdict"),
            det.get("safe_inputs", 0), det.get("n", 0))
    if lever == "cache":
        return "verdict **%s**" % ("recommended" if det.get("recommend") else "not recommended")
    tok = (" · %s → %s tokens" % (det.get("before_tok"), det.get("after_tok"))) if det.get("after_tok") else ""
    return "verdict **%s**%s" % (det.get("verdict", "?"), tok)


def _runs_block(L, title, runs, tag_true, tag_false):
    """A labelled list of outputs, each tagged (+ votes / reason), one fenced block per output. Full text — this is
    the proof; truncating it defeats the purpose."""
    if not runs:
        return
    L += ["", "**%s**" % title]
    for i, r in enumerate(runs, 1):
        vt = (" · %s" % r["votes"]) if r.get("votes") else ""
        rs = (" — %s" % r["reason"]) if r.get("reason") else ""
        L += ["", "%d · _%s_%s%s" % (i, tag_true if r.get("preserved") else tag_false, vt, rs),
              "```text", (r.get("output") or "(empty)"), "```"]


def _evidence_input(L, i, inp, cheaper):
    req = (inp.get("input") or "").strip().replace("\n", " ")
    L += ["", "#### input %d — %s" % (i, (req[:100] or "(no user text)")),
          "", "- verdict: **%s** · %s" % (_IV.get(inp["verdict"], inp["verdict"]), _rate(inp))]
    if inp.get("note") or inp.get("reason"):
        L.append("- judge: %s" % (inp.get("note") or inp.get("reason")))
    if inp.get("orig_runs"):                                   # refset downgrade: reference set vs cheaper
        _runs_block(L, "Reference set — your current model's own outputs", inp["orig_runs"], "fits", "differs")
        title = "Cheaper model's outputs" + ((" (`%s`)" % cheaper) if cheaper else "")
        _runs_block(L, title, inp.get("samples", []), "fits", "differs")
    else:                                                      # legacy cache/compress: recorded original vs re-runs
        L += ["", "**Recorded original**", "```text", (inp.get("recorded") or "(empty)"), "```"]
        _runs_block(L, "Re-runs", inp.get("samples", []), "kept", "drift")


def _evidence_md(f, proof):
    """The raw runs behind every verdict — for verification, not application. Every audited call-site×lever with
    per-input data is shown (fixes AND rejections), so a skeptic can reproduce any verdict."""
    results = proof["results"] if proof else []
    L = ["# Token Audit — %s · Evidence" % f["agent"], "",
         "> The raw runs behind every verdict in `report.md` — read this to **verify** a recommendation; `report.md` "
         "alone is enough to **apply** one. Each re-run was judged live against real recorded inputs; nothing here is "
         "invented.", ""]
    any_ev = False
    for x in results:
        key = x.get("key") or x["node"]
        for lever in ("downgrade", "cache", "compress"):
            det = x.get(lever)
            if not det or (lever == "cache" and det.get("informational")):
                continue
            inputs = det.get("inputs") or []
            if not inputs:
                continue
            any_ev = True
            L += ["## `%s` — %s" % (key, _EV_LABEL[lever]), "", _ev_headline(lever, det, x)]
            sysm = _first_system(inputs)
            if sysm:
                L += ["", "System prompt (constant for this call-site):", "```text", sysm, "```"]
            for i, inp in enumerate(inputs, 1):
                _evidence_input(L, i, inp, det.get("cheaper"))
            L.append("")
    if not any_ev:
        L.append("_No per-input evidence was captured — nothing was audited live._")
    return "\n".join(L) + "\n"


def build_zip(f, proof, html=None):
    """Return the audit record as ZIP bytes: report.html (the self-contained styled report — opens anywhere, prints
    to PDF from the browser; included when the caller renders it), report.md (the deliverable) and evidence.md (the
    proof appendix, only when there is per-input evidence to show)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if html:
            z.writestr("report.html", html)
        z.writestr("report.md", _report_md(f, proof))
        has_ev = any((x.get(lv) or {}).get("inputs")
                     for x in (proof["results"] if proof else [])
                     for lv in ("downgrade", "cache", "compress")
                     if not (lv == "cache" and (x.get(lv) or {}).get("informational")))
        if has_ev:
            z.writestr("evidence.md", _evidence_md(f, proof))
    return buf.getvalue()
