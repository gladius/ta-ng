"""Token Auditor — minimal FastAPI + Jinja backend.

Slice 1 (only): datasource -> workspaces -> agents. Three read-only navigation pages, nothing else.
Run:  python -m app.web.server        (http://127.0.0.1:8100, live-reload on)
      uvicorn app.web.server:app --port 8100
"""
import os
import json

from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services import nav
from app.config import LEVERS      # enabled levers (default: downgrade only on this branch)

_HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Token Auditor")
app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))


@app.get("/healthz/db")
def healthz_db():
    """Liveness + which store backend is actually in use — 'postgresql' or 'sqlite', plus target host/db (no creds),
    entry count, and last write. 503 if the DB can't be reached, so uptime monitors catch a broken store."""
    from app.services import db
    h = db.health()
    return JSONResponse(h, status_code=200 if h.get("ok") else 503)


_CSS_PATH = os.path.join(_HERE, "static", "app.css")


def _page(request, name, **ctx):
    # cache-bust the stylesheet by its mtime: any CSS edit changes the URL, so a browser can never serve a stale
    # app.css (which would leave var(--token) gaps unresolved). Costs one stat per render — negligible.
    try:
        ctx.setdefault("css_v", int(os.path.getmtime(_CSS_PATH)))
    except OSError:
        ctx.setdefault("css_v", 1)
    return templates.TemplateResponse(request, name, ctx)   # Starlette signature: (request, name, context)


def _sse(event, data):
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data))


@app.get("/", response_class=HTMLResponse)
def datasources(request: Request):
    return _page(request, "datasource.html", datasources=nav.datasources())


@app.get("/s/{source}/workspaces", response_class=HTMLResponse)
def workspaces(request: Request, source: str):
    return _page(request, "workspaces.html", source=source, workspaces=nav.workspaces(source))


@app.get("/s/{source}/ws/{ws_id}/agents", response_class=HTMLResponse)
def agents(request: Request, source: str, ws_id: str):
    return _page(request, "agents.html", source=source, ws_id=ws_id,
                 ws_name=nav.workspace_name(source, ws_id), agents=nav.agents(source, ws_id))


# ── Savings report: the deliverable. Funnel (free) → prove selected (paid, SSE) → frozen report → download ──
@app.get("/models", response_class=HTMLResponse)
def models_view(request: Request):
    """The CONSOLIDATED model registry — the in-memory reference (config/models.json) × what the gateway actually
    serves (availability). Read-only. Availability is real only with a gateway; 'unknown' in dev."""
    from app import registry
    return _page(request, "models.html", reg=registry.report())


@app.get("/migration-path/{provider}", response_class=HTMLResponse)
def migration_path_view(request: Request, provider: str):
    """The vendor's recommended migration paths for a provider (config/migrations/<provider>.json) — from→to."""
    from app.migration import migration_paths
    paths = [p for p in migration_paths() if (p.get("provider") or "").lower() == provider.lower()]
    return _page(request, "migration_path.html", provider=provider, paths=paths)


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/report", response_class=HTMLResponse)
def report_view(request: Request, source: str, ws_id: str, project: str, snap: str = "", cache: int = 0):
    from app.services import funnel, prove, store, snapshot
    g = snapshot.get(snap, source, ws_id, project) if snap else None
    if g is None:                        # no/unknown/evicted snap -> mint an id, show a PROGRESS page, build via SSE.
        sid = snapshot.new_id()          # (the multi-second fetch used to block here as a frozen page — now it streams)
        return _page(request, "building.html", source=source, ws_id=ws_id, project=project,
                     snap=sid, cache=int(cache), agent=project)
    proof = store.peek(prove.proof_key(source, ws_id, project, snap))             # audited result, tied to THIS snapshot
    if proof is None:                    # built but not audited yet -> pick call-sites first (report = results only)
        return RedirectResponse("/s/%s/ws/%s/agent/%s/select?snap=%s"
                                % (source, ws_id, quote(project, safe=""), snap), status_code=303)
    dgmode = proof.get("dgmode", "commercial")                   # SAME universe the proof was run under
    f = funnel.build(source, ws_id, project, levers=LEVERS, g=g, dgmode=dgmode)  # all levers, PINNED snapshot
    results = proof["results"]                                                    # STRATEGY-primary report: one section
    dg = [r for r in results if r.get("downgrade")]                               # per lever, each listing only the
    ca = [r for r in results if r.get("cache") and not r["cache"].get("informational")]  # call-sites it applies to
    co = [r for r in results if r.get("compress")]                               # compression: the call-sites it ran on
    return _page(request, "report.html", source=source, ws_id=ws_id, project=project,
                 f=f, proof=proof, dg=dg, ca=ca, co=co, snap=snap, levers=LEVERS)


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/profile", response_class=HTMLResponse)
def profile_view(request: Request, source: str, ws_id: str, project: str, snap: str = "", skip: int = 0):
    """Deployment PROFILE — a read-only VIEW of a PINNED snapshot (identity can't drift). It never builds
    inline: no/unknown snap -> mint an id and build ONCE via the shared SSE flow (building.html), landing
    back here on the same snap. The per-node analysis is generated ON THE WAY IN (analyzing.html progress
    page) rather than behind a button — so arriving at the profile means it's already there. `skip=1` is the
    escape hatch (from the progress page's failure state) to view the deterministic profile without it."""
    from app.services import snapshot, store
    from app.services.profile.assemble import build_profile
    from app.services.profile import comprehend
    g = snapshot.get(snap, source, ws_id, project) if snap else None
    if g is None:
        sid = snapshot.new_id()
        return _page(request, "building.html", source=source, ws_id=ws_id, project=project,
                     snap=sid, agent=project, next="profile")
    comp = store.peek(comprehend.comprehend_key(source, ws_id, project, snap))   # advisory layer, if already run
    if comp is None and not skip:                        # not computed yet -> run it on a progress page, then land here
        return _page(request, "analyzing.html", source=source, ws_id=ws_id, project=project, snap=snap, agent=project)
    return _page(request, "profile.html", source=source, ws_id=ws_id, project=project, snap=snap,
                 prof=build_profile(g, comp=comp))


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/comprehend-stream")
def comprehend_stream(source: str, ws_id: str, project: str, snap: str = ""):
    """SSE — compute the per-node comprehension (op + summary) + a deployment summary over the PINNED snapshot,
    node by node so the wait is visible, then FREEZE it. Paid: one small PROFILE_MODEL call per llm node + one
    synthesis call. The report re-renders with it on reload. Sync endpoint -> threadpool, never blocks the loop."""
    from app.services import snapshot
    from app.services.profile import comprehend

    def gen():
        g = snapshot.get(snap, source, ws_id, project)
        if g is None:
            yield _sse("failed", {"msg": "snapshot expired — reopen the profile to rebuild"})
            return
        try:
            for evt in comprehend.stream(source, ws_id, project, snap,
                                         g.get("nodes", []), g.get("buckets", {}), g.get("edges", []),
                                         g.get("structural_nodes", [])):
                yield _sse(evt.get("type", "stage"), evt)
        except Exception as e:
            yield _sse("failed", {"msg": str(e)[:300]})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/build-stream")
def build_stream(source: str, ws_id: str, project: str, snap: str, next: str = ""):
    """SSE — build the graph ONCE (pinned under `snap`), streaming coarse progress so the wait is visible (not a
    frozen page), then hand back the url for this snap. `next=profile` lands on the profile view; otherwise the
    select workbench (default). `failed` on a build error. Sync endpoint => runs in the threadpool, never blocks
    the loop. The heavy work is one bulk pull (~3 paginated calls) + fold."""
    from app.services import snapshot

    def gen():
        yield _sse("stage", {"msg": "Fetching traces & grouping call-sites…", "pct": 25})
        try:
            g = snapshot.build_into(snap, source, ws_id, project)
        except Exception as e:                                       # surface the real reason, don't 500 a blank page
            yield _sse("failed", {"msg": str(e)[:300]})
            return
        yield _sse("stage", {"msg": "Found %d call-sites across %d traces"
                             % (len(g.get("nodes", [])), g.get("traces", 0)), "pct": 92})
        target = "profile" if next == "profile" else "select"
        base = "/s/%s/ws/%s/agent/%s/%s" % (source, ws_id, quote(project, safe=""), target)
        yield _sse("done", {"url": "%s?snap=%s" % (base, snap)})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/select", response_class=HTMLResponse)
def select_view(request: Request, source: str, ws_id: str, project: str, snap: str = "", dgmode: str = "commercial"):
    """The selection workbench — pick which call-sites to audit (top-5 costliest or manual graph+nodes), then
    prove them. Reads the PINNED snapshot so identity can't drift. No snap -> go build one. `dgmode` toggles the
    downgrade target universe ('commercial' | 'open-weight'); flipping it re-renders the picks and is carried into
    the audit so the proof re-runs the same targets."""
    from app.services import funnel, snapshot
    g = snapshot.get(snap, source, ws_id, project) if snap else None
    if g is None:
        return RedirectResponse("/s/%s/ws/%s/agent/%s/report" % (source, ws_id, quote(project, safe="")),
                                status_code=303)
    dgmode = "open-weight" if dgmode == "open-weight" else "commercial"        # validate -> only the two lanes
    f = funnel.build(source, ws_id, project, levers=LEVERS, g=g, dgmode=dgmode)
    return _page(request, "select.html", source=source, ws_id=ws_id, project=project, f=f, snap=snap,
                 levers=LEVERS, dgmode=dgmode)


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/auditing", response_class=HTMLResponse)
def auditing_view(request: Request, source: str, ws_id: str, project: str, snap: str = "", keys: str = "",
                  levers: str = "", dgmode: str = "commercial"):
    """Audit-progress page — proves the SELECTED `keys` with the SELECTED `levers` (via prove-stream) with a live
    N/M list, then redirects to the report. No snap -> go rebuild. `dgmode` is passed straight through to the
    prove stream so the proof uses the SAME target universe the select page showed."""
    from app.services import snapshot
    if snapshot.get(snap, source, ws_id, project) is None:
        return RedirectResponse("/s/%s/ws/%s/agent/%s/report" % (source, ws_id, quote(project, safe="")),
                                status_code=303)
    return _page(request, "auditing.html", source=source, ws_id=ws_id, project=project,
                 snap=snap, keys=keys, levers=levers, dgmode=dgmode, agent=project)


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/prove-stream")
def prove_stream(source: str, ws_id: str, project: str, keys: str, snap: str = "", levers: str = "",
                 dgmode: str = "commercial"):
    """SSE — prove the selected call-sites with the selected LEVERS live against the PINNED snapshot. Paid; freezes
    the result for the report. `levers` empty -> all three (prove.stream's default). `dgmode` = downgrade target
    universe, carried from select so the proof re-runs the model the user actually saw."""
    from app.services import prove
    keylist = [k for k in keys.split(",") if k]
    leverlist = [x for x in levers.split(",") if x] or None
    dgmode = "open-weight" if dgmode == "open-weight" else "commercial"

    def gen():
        for evt in prove.stream(source, ws_id, project, keylist, snap, levers=leverlist, dgmode=dgmode):
            yield "event: %s\ndata: %s\n\n" % (evt.get("type", "msg"), json.dumps(evt))

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/s/{source}/ws/{ws_id}/agent/{project}/prove-clear")
def prove_clear(source: str, ws_id: str, project: str, snap: str = ""):
    from app.services import prove, store
    store.clear(prove.proof_key(source, ws_id, project, snap))
    return {"ok": True}


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/report/download")
def report_download(source: str, ws_id: str, project: str, snap: str = ""):
    """Download the audit record as a ZIP — report.html (the self-contained styled report, opens anywhere & prints to
    PDF), report.md (the apply guide for a dev/coding assistant) and evidence.md (proof appendix). Frozen proof only."""
    from fastapi.responses import Response
    from app.services import funnel, prove, store, report_doc, snapshot
    from app.web.export import render as export_render
    g = snapshot.get(snap, source, ws_id, project)
    proof = store.peek(prove.proof_key(source, ws_id, project, snap))
    dgmode = (proof or {}).get("dgmode", "commercial")           # match the proof's target universe
    f = funnel.build(source, ws_id, project, levers=LEVERS, g=g, dgmode=dgmode)
    html = None
    if g is not None and proof is not None:                                   # render the same report page, portably
        results = proof["results"]
        dg = [r for r in results if r.get("downgrade")]
        ca = [r for r in results if r.get("cache") and not r["cache"].get("informational")]
        co = [r for r in results if r.get("compress")]
        html = export_render(templates, "report.html",
                             dict(source=source, ws_id=ws_id, project=project,
                                  f=f, proof=proof, dg=dg, ca=ca, co=co, snap=snap, levers=LEVERS))
    data = report_doc.build_zip(f, proof, html=html)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="token-audit-%s.zip"' % project})


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/profile/download")
def profile_download(source: str, ws_id: str, project: str, snap: str = ""):
    """Download the deployment profile as a ZIP — profile.html (self-contained styled profile, opens anywhere & prints
    to PDF) + profile.md (a compact markdown summary). Built from the pinned snapshot + advisory analysis if present."""
    from fastapi.responses import Response
    from app.services import snapshot, store, profile_doc
    from app.services.profile.assemble import build_profile
    from app.services.profile import comprehend
    from app.web.export import render as export_render
    g = snapshot.get(snap, source, ws_id, project)
    comp = store.peek(comprehend.comprehend_key(source, ws_id, project, snap)) if g is not None else None
    prof = build_profile(g, comp=comp) if g is not None else {}
    html = export_render(templates, "profile.html",
                         dict(source=source, ws_id=ws_id, project=project, snap=snap, prof=prof)) if g is not None else None
    data = profile_doc.build_zip(prof, html=html)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="token-profile-%s.zip"' % project})


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8100"))
    uvicorn.run("app.web.server:app", host=host, port=port, reload=True)
