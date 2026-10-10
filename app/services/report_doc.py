"""Report export: a ZIP of two markdown files built only from the frozen proof (no LLM, so it is reproducible).
report.md is the self-contained apply guide; evidence.md holds the raw runs behind each verdict, for verifying.
"""
import io
import json
import re
import zipfile

# per-input verdict labels
_IV = {"SAFE": "safe", "NOT-SAFE": "not safe", "BORDERLINE": "borderline", "LOW-EVIDENCE": "low evidence"}

def _fmt(n):
    return "{:,.0f}".format(n or 0)


def _sys_first_line(detail):
    """The first non-blank line of the call-site's system prompt: a greppable way to find it in the source."""
    inps = detail.get("inputs") or []
    sysm = (inps[0].get("system") if inps else "") or ""
    return next((ln.strip() for ln in sysm.splitlines() if ln.strip()), "")[:140]


def _drift(detail):
    """Why a downgrade was rejected: the first NOT-SAFE input's judge reason, else the finding's own reason."""
    for r in detail.get("inputs") or []:
        if r.get("verdict") == "NOT-SAFE":
            return r.get("note") or r.get("reason") or ""
    return detail.get("reason") or ""


def _rate(inp):
    """Per-input rate: the cheaper model's fit to the reference set · the original's self-consistency."""
    return "cheaper %d/%d · original %s" % (inp.get("kept", 0), inp.get("k", 0), inp.get("self_rate", "?"))


# ─────────────────────────────────────────── report.md — the apply guide ───────────────────────────────────────────

def _report_md(f, proof):
    """report.md: one apply block per recommended fix, a machine-readable index, and what was tested and rejected."""
    calls = f["calls"]
    unit = "%s calls" % _fmt(calls)
    results = proof["results"] if proof else []
    total = (proof.get("total", 0) if proof else 0)

    fixes, rejects = [], []                     # fixes: (x, detail, usd)   rejects: (call_site, verdict, reason)
    for x in results:
        key = x.get("key") or x["node"]
        d = x.get("downgrade")
        if d and d.get("verdict") == "SAFE":
            fixes.append((x, d, x.get("downgrade_usd", 0)))
        elif d:
            rejects.append((key, d.get("verdict", "?"), _drift(d)))

    L = ["# Token Audit — %s · Apply Guide" % f["agent"], ""]
    L.append("> These are **verified, behaviour-preserving** changes to your agent, each proven on real recorded "
             "inputs. Apply each at the named call-site **exactly as written** — the prompts and prefixes below were "
             "proven verbatim, so editing a proven artifact voids its proof. Advisory & read-only: you decide what to "
             "apply. The raw runs behind every verdict are in **`evidence.md`** — you need only this file to apply.")
    L.append("")
    if total > 0:
        L.append("**$%s in verified savings per %s** from model downgrades." % (_fmt(total), unit))
    else:
        L.append("**No safe savings on the audited call-sites** — each re-ran live and needs its current setup. A "
                 "verified result, not an empty one; see *Tested and not recommended* below.")
    L.append("")
    L.append("Audited **%d call-site(s)** across **%s traces**; **%d fix(es) to apply**. All $ per %s."
             % (len(results), f.get("traces", 0), len(fixes), unit))
    L.append("")

    # ── machine-readable index, for a coding assistant ──
    idx = []
    for n, (x, det, usd) in enumerate(fixes, 1):
        idx.append({"fix": n, "lever": "downgrade", "call_site": (x.get("key") or x["node"]), "node": x["node"],
                    "graph_path": x.get("graph_path", ""), "saving": round(usd, 2), "basis": unit,
                    "from_model": det.get("model"), "to_model": det.get("cheaper")})
    L += ["## Fixes — machine-readable index", "```json", json.dumps(idx, indent=2), "```", ""]

    # ── one apply block per fix ──
    if fixes:
        L += ["## Fixes to apply", ""]
    for n, (x, det, usd) in enumerate(fixes, 1):
        key = x.get("key") or x["node"]
        L.append("### Fix %d · Model downgrade · `%s` — saves $%s per %s" % (n, key, _fmt(usd), unit))
        L.append("")
        L.append("**Where**")
        L.append("- Call-site: node `%s`%s, agent `%s`, currently on model `%s`." % (
            x["node"], (" (subgraph `%s`)" % x["graph_path"]) if x.get("graph_path") else "", f["agent"], x["model"]))
        fp = _sys_first_line(det)
        if fp:
            L += ["- Find it by the node name, or by its system prompt which begins:", "  > %s" % fp]
        L += ["", "**Change**",
              "- Switch this call-site's model:  `%s`  →  `%s`" % (det.get("model"), det.get("cheaper")),
              "- Nothing else changes — same prompt, same tools."]
        ni, si, k = det.get("n", 0), det.get("safe_inputs", 0), det.get("k", 0)
        L += ["", "**Why it's safe**"]
        L.append("- Re-ran %d distinct real input(s). On each, we ran your current model %d times to capture its "
                     "own outputs — the **reference set**, i.e. the behaviour and the natural variation it already "
                     "shows — then ran `%s` and judged each of its outputs against that set. It stayed within the set "
                     "on **%d/%d**. The cheaper model is only held to the latitude your current model itself takes, so "
                 "it is never penalised for variation the original already shows." % (ni, k, det.get("cheaper"), si, ni))
        L.append("- Saves **$%s per %s**." % (_fmt(usd), unit))
        L += ["", "**Verify & caveats**",
              "- After applying, re-audit this call-site (or re-run these inputs and diff behaviour). Full proof: "
              "**`evidence.md`** → `%s` (model downgrade)." % key, "", "---", ""]

    # ── tested and rejected, so nothing is silently touched ──
    if rejects:
        L += ["## Tested and not recommended (keep current)",
              "We tried these and the proof said no — listed so you know what was tested and *not* to touch. The runs "
              "behind each are in `evidence.md`:"]
        for key, verdict, reason in rejects:
            L.append("- `%s` · model downgrade → **%s**%s" % (key, verdict, (" — %s" % reason) if reason else ""))
        L.append("")

    L.append("---")
    L.append("_Figures per %s — a fixed basis, not a monthly total; multiply by your real call volume to scale. Built "
             "only from the frozen proof: measured live against your recorded traffic, nothing invented._" % unit)
    return "\n".join(L) + "\n"


# ────────────────────────────────────────── evidence.md — the proof appendix ───────────────────────────────────────

def _first_system(inputs):
    return next((inp["system"] for inp in inputs if inp.get("system")), "")


def _ev_headline(det, x):
    """The one-line summary under a call-site heading in evidence.md."""
    return "current `%s` → `%s` · verdict **%s** (%d/%d input(s) safe)" % (
        det.get("model") or x.get("model"), det.get("cheaper"), det.get("verdict"),
        det.get("safe_inputs", 0), det.get("n", 0))


def _runs_block(L, title, runs, tag_true, tag_false):
    """Outputs as a tagged list (votes, reason), one fenced block each, untruncated: this is the proof."""
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
    _runs_block(L, "Reference set — your current model's own outputs", inp.get("orig_runs", []), "fits", "differs")
    title = "Cheaper model's outputs" + ((" (`%s`)" % cheaper) if cheaper else "")
    _runs_block(L, title, inp.get("samples", []), "fits", "differs")


def _evidence_md(f, proof):
    """evidence.md: the raw runs behind every verdict, fixes and rejections alike, so any verdict can be checked."""
    results = proof["results"] if proof else []
    L = ["# Token Audit — %s · Evidence" % f["agent"], "",
         "> The raw runs behind every verdict in `report.md` — read this to **verify** a recommendation; `report.md` "
         "alone is enough to **apply** one. Each re-run was judged live against real recorded inputs; nothing here is "
         "invented.", ""]
    any_ev = False
    for x in results:
        key = x.get("key") or x["node"]
        det = x.get("downgrade") or {}
        inputs = det.get("inputs") or []
        if not inputs:
            continue
        any_ev = True
        L += ["## `%s` — model downgrade" % key, "", _ev_headline(det, x)]
        sysm = _first_system(inputs)
        if sysm:
            L += ["", "System prompt (constant for this call-site):", "```text", sysm, "```"]
        for i, inp in enumerate(inputs, 1):
            _evidence_input(L, i, inp, det.get("cheaper"))
        L.append("")
    if not any_ev:
        L.append("_No per-input evidence was captured — nothing was audited live._")
    return "\n".join(L) + "\n"


def build_zip(f, proof):
    """The audit record as ZIP bytes: report.md, plus evidence.md when there is per-input evidence."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.md", _report_md(f, proof))
        has_ev = any((x.get("downgrade") or {}).get("inputs") for x in (proof["results"] if proof else []))
        if has_ev:
            z.writestr("evidence.md", _evidence_md(f, proof))
    return buf.getvalue()
