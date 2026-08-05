"""Token Auditor — minimal FastAPI + Jinja backend.

Slice 1 (only): datasource -> workspaces -> agents. Three read-only navigation pages, nothing else.
Run:  python -m app.web.server        (http://127.0.0.1:8100, live-reload on)
      uvicorn app.web.server:app --port 8100
"""
import os
import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services import nav

_HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Token Auditor")
app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))


def _page(request, name, **ctx):
    return templates.TemplateResponse(request, name, ctx)   # Starlette signature: (request, name, context)


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
def report_view(request: Request, source: str, ws_id: str, project: str):
    from app.services import funnel, prove, store
    f = funnel.build(source, ws_id, project)                                      # recompute every view: $0,
    #                                                    deterministic → always fresh, never a stagnant snapshot
    proof = store.peek(prove.proof_key(source, ws_id, project))                   # paid; shown only if explicitly run
    return _page(request, "report.html", source=source, ws_id=ws_id, project=project, f=f, proof=proof)


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/prove-stream")
def prove_stream(source: str, ws_id: str, project: str, keys: str):
    """SSE — prove the selected call-sites live, streaming progress. Paid; freezes the result for the report."""
    from app.services import prove
    keylist = [k for k in keys.split(",") if k]

    def gen():
        for evt in prove.stream(source, ws_id, project, keylist):
            yield "event: %s\ndata: %s\n\n" % (evt.get("type", "msg"), json.dumps(evt))

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/s/{source}/ws/{ws_id}/agent/{project}/prove-clear")
def prove_clear(source: str, ws_id: str, project: str):
    from app.services import prove, store
    store.clear(prove.proof_key(source, ws_id, project))
    return {"ok": True}


@app.get("/s/{source}/ws/{ws_id}/agent/{project}/report/download")
def report_download(source: str, ws_id: str, project: str):
    """Download the audit record as a ZIP (folder of small files) — scales for large agents. Frozen proof only."""
    from fastapi.responses import Response
    from app.services import funnel, prove, store, report_doc
    proof = store.peek(prove.proof_key(source, ws_id, project))
    f = funnel.build(source, ws_id, project)
    data = report_doc.build_zip(f, proof)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="token-audit-%s.zip"' % project})


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8100"))
    uvicorn.run("app.web.server:app", host=host, port=port, reload=True)
