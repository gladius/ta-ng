"""Token Auditor — minimal FastAPI + Jinja backend.

Slice 1 (only): datasource -> workspaces -> agents. Three read-only navigation pages, nothing else.
Run:  python -m app.web.server        (http://127.0.0.1:8100, live-reload on)
      uvicorn app.web.server:app --port 8100
"""
import os
import json

from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services import nav

_HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Token Auditor")
app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))


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
    f = funnel.build(source, ws_id, project, levers=["downgrade", "cache"], g=g)  # both levers, from the PINNED snapshot
    results = proof["results"]                                                    # STRATEGY-primary report: one section
    dg = [r for r in results if r.get("downgrade")]                               # per lever, each listing only the
    ca = [r for r in results if r.get("cache") and not r["cache"].get("informational")]  # call-sites it applies to
    return _page(request, "report.html", source=source, ws_id=ws_id, project=project,
                 f=f, proof=proof, dg=dg, ca=ca, snap=snap)


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/build-stream")
def build_stream(source: str, ws_id: str, project: str, snap: str):
    """SSE — build the graph ONCE (pinned under `snap`), streaming coarse progress so the wait is visible (not a
    frozen page), then hand back the SELECT url for this snap. `failed` on a build error. Sync endpoint => runs in
    the threadpool, never blocks the loop. The heavy work is one bulk pull (~3 paginated calls) + fold."""
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
        base = "/s/%s/ws/%s/agent/%s/select" % (source, ws_id, quote(project, safe=""))
        yield _sse("done", {"url": "%s?snap=%s" % (base, snap)})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/select", response_class=HTMLResponse)
def select_view(request: Request, source: str, ws_id: str, project: str, snap: str = ""):
    """The selection workbench — pick which call-sites to audit (top-5 costliest or manual graph+nodes), then
    prove them. Reads the PINNED snapshot so identity can't drift. No snap -> go build one."""
    from app.services import funnel, snapshot
    g = snapshot.get(snap, source, ws_id, project) if snap else None
    if g is None:
        return RedirectResponse("/s/%s/ws/%s/agent/%s/report" % (source, ws_id, quote(project, safe="")),
                                status_code=303)
    f = funnel.build(source, ws_id, project, levers=["downgrade", "cache"], g=g)
    return _page(request, "select.html", source=source, ws_id=ws_id, project=project, f=f, snap=snap)


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/auditing", response_class=HTMLResponse)
def auditing_view(request: Request, source: str, ws_id: str, project: str, snap: str = "", keys: str = ""):
    """Audit-progress page — proves the SELECTED `keys` (via prove-stream) with a live N/M list, then redirects to
    the report. No snap -> go rebuild."""
    from app.services import snapshot
    if snapshot.get(snap, source, ws_id, project) is None:
        return RedirectResponse("/s/%s/ws/%s/agent/%s/report" % (source, ws_id, quote(project, safe="")),
                                status_code=303)
    return _page(request, "auditing.html", source=source, ws_id=ws_id, project=project,
                 snap=snap, keys=keys, agent=project)


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/prove-stream")
def prove_stream(source: str, ws_id: str, project: str, keys: str, snap: str = ""):
    """SSE — prove the selected call-sites live against the PINNED snapshot. Paid; freezes the result for the report."""
    from app.services import prove
    keylist = [k for k in keys.split(",") if k]

    def gen():
        for evt in prove.stream(source, ws_id, project, keylist, snap):
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
    """Download the audit record as a ZIP (folder of small files) — scales for large agents. Frozen proof only."""
    from fastapi.responses import Response
    from app.services import funnel, prove, store, report_doc, snapshot
    g = snapshot.get(snap, source, ws_id, project)
    proof = store.peek(prove.proof_key(source, ws_id, project, snap))
    f = funnel.build(source, ws_id, project, g=g)
    data = report_doc.build_zip(f, proof)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="token-audit-%s.zip"' % project})


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8100"))
    uvicorn.run("app.web.server:app", host=host, port=port, reload=True)
