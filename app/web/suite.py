"""JSON the web UI needs beyond the validation API: the home page's facts, browsing sources and agents, and an
agent's audit (snapshot, select, prove, report, profile; the long steps as server-sent event streams). Same sign-in as
the API (app/web/validations.require_key), but not part of /openapi.json, which documents the validation API for
other teams."""
import json
import re

from fastapi import APIRouter, Depends, HTTPException

from app import registry
from app.web.validations import require_key

api = APIRouter(prefix="/api/suite", dependencies=[Depends(require_key)], include_in_schema=False)


# ── browse: data sources -> workspaces -> agents, and the model catalog ─────────────────────────────────────────────
@api.get("/sources")
def sources():
    from app.services import nav
    return nav.datasources()


@api.get("/sources/{source}/workspaces")
def workspaces(source: str):
    from app.services import nav
    return nav.workspaces(source)


@api.get("/sources/{source}/workspaces/{ws}/agents")
def agents(source: str, ws: str):
    from app.services import nav
    return {"name": nav.workspace_name(source, ws), "agents": nav.agents(source, ws)}


@api.get("/models")
def models():
    """The model catalog (config/models.json) consolidated with what the gateway serves."""
    return registry.report()


@api.get("/migrations/{provider}")
def migrations(provider: str):
    """The vendor's recommended migration paths for a provider (config/migrations/<provider>.json)."""
    from app.migration import migration_paths
    return [p for p in migration_paths() if (p.get("provider") or "").lower() == provider.lower()]


# ── one agent's audit, on a pinned snapshot: build -> select -> prove -> report; profile alongside ────────────────────
AGENT = "/agents/{source}/{ws}/{project:path}"          # a project name may hold "/"


def _sse(events):
    """A generator of event dicts ({"type": ..., ...}) as a server-sent event stream."""
    from fastapi.responses import StreamingResponse
    body = ("event: %s\ndata: %s\n\n" % (e.get("type", "stage"), json.dumps(e)) for e in events)
    return StreamingResponse(body, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _attachment(name):
    """A download's Content-Disposition for any agent name: an ASCII fallback, and the real name (RFC 5987)."""
    from urllib.parse import quote
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (re.sub(r"[^A-Za-z0-9._-]", "_", name), quote(name, safe=""))


def _snapshot(source, ws, project, snap):
    from app.services import snapshot
    g = snapshot.get(snap, source, ws, project) if snap else None
    if g is None:
        raise HTTPException(404, "no such snapshot: build one first")
    return g


@api.post(AGENT + "/snapshots")
def new_snapshot(source: str, ws: str, project: str):
    """A fresh snapshot id; build-stream fills it."""
    from app.services import snapshot
    return {"snap": snapshot.new_id()}


@api.get(AGENT + "/build-stream")
def build_stream(source: str, ws: str, project: str, snap: str):
    """SSE: pull the agent's traces and group its call-sites ONCE, pinned under `snap` (stage, done, failed)."""
    from app.services import snapshot

    def events():
        yield {"type": "stage", "msg": "Fetching traces & grouping call-sites…", "pct": 25}
        try:
            g = snapshot.build_into(snap, source, ws, project)
        except Exception as e:                                       # the real reason, not a blank failure
            yield {"type": "failed", "msg": str(e)[:300]}
            return
        yield {"type": "stage", "msg": "Found %d call-sites across %d traces" % (len(g.get("nodes", [])),
                                                                                 g.get("traces", 0)), "pct": 92}
        yield {"type": "done", "snap": snap}
    return _sse(events())


@api.get(AGENT + "/select")
def select(source: str, ws: str, project: str, snap: str, dgmode: str = "commercial"):
    """The call-sites to choose from, costliest first, with each one's downgrade target in `dgmode`
    ('commercial': one tier down or the vendor's path; 'open-weight': the same-tier open-weight peer)."""
    from app.services import funnel
    g = _snapshot(source, ws, project, snap)
    return funnel.build(source, ws, project, g=g, dgmode="open-weight" if dgmode == "open-weight" else "commercial")


@api.get(AGENT + "/prove-stream")
def prove_stream(source: str, ws: str, project: str, snap: str, keys: str, dgmode: str = "commercial"):
    """SSE: prove the selected call-sites live (paid) and freeze the result for the report
    (start, step, done_node, complete)."""
    from app.services import prove

    def events():
        try:
            yield from prove.stream(source, ws, project, [k for k in keys.split(",") if k], snap,
                                    dgmode="open-weight" if dgmode == "open-weight" else "commercial")
        except Exception as e:                                       # the real reason, not a dropped connection
            yield {"type": "failed", "msg": str(e)[:300]}
    return _sse(events())


@api.get(AGENT + "/report")
def report(source: str, ws: str, project: str, snap: str):
    """The audited result on this snapshot: 404 without the snapshot, 409 when nothing was audited on it yet."""
    from app.services import funnel, prove, store
    g = _snapshot(source, ws, project, snap)
    proof = store.peek(prove.proof_key(source, ws, project, snap))
    if proof is None:
        raise HTTPException(409, "not audited yet: choose call-sites first")
    f = funnel.build(source, ws, project, g=g, dgmode=proof.get("dgmode", "commercial"))
    return {"funnel": f, "proof": proof}


@api.post(AGENT + "/prove-clear")
def prove_clear(source: str, ws: str, project: str, snap: str = ""):
    from app.services import prove, store
    store.clear(prove.proof_key(source, ws, project, snap))
    return {"ok": True}


@api.get(AGENT + "/profile")
def profile(source: str, ws: str, project: str, snap: str):
    """The deployment profile of this snapshot, with the per-node analysis when it has been run."""
    from app.services import store
    from app.services.profile import comprehend
    from app.services.profile.assemble import build_profile
    g = _snapshot(source, ws, project, snap)
    comp = store.peek(comprehend.comprehend_key(source, ws, project, snap))
    return {"profile": build_profile(g, comp=comp), "analysed": comp is not None}


@api.get(AGENT + "/comprehend-stream")
def comprehend_stream(source: str, ws: str, project: str, snap: str):
    """SSE: read each call-site once to describe what it does, plus a deployment summary (paid; stage, done, failed)."""
    from app.services import snapshot
    from app.services.profile import comprehend

    def events():
        g = snapshot.get(snap, source, ws, project)
        if g is None:
            yield {"type": "failed", "msg": "the snapshot expired: open the agent again to rebuild it"}
            return
        try:
            yield from comprehend.stream(source, ws, project, snap, g.get("nodes", []), g.get("buckets", {}),
                                         g.get("edges", []), g.get("structural_nodes", []))
        except Exception as e:
            yield {"type": "failed", "msg": str(e)[:300]}
    return _sse(events())


@api.get(AGENT + "/report.zip")
def report_zip(source: str, ws: str, project: str, snap: str):
    """The audit record to keep or hand on: report.md and, when there is evidence, evidence.md."""
    from fastapi.responses import Response
    from app.services import funnel, prove, report_doc, store
    g = _snapshot(source, ws, project, snap)
    proof = store.peek(prove.proof_key(source, ws, project, snap))
    f = funnel.build(source, ws, project, g=g, dgmode=(proof or {}).get("dgmode", "commercial"))
    return Response(report_doc.build_zip(f, proof), media_type="application/zip",
                    headers={"Content-Disposition": _attachment("token-audit-%s.zip" % project)})


@api.get(AGENT + "/profile.zip")
def profile_zip(source: str, ws: str, project: str, snap: str):
    """The deployment profile as profile.md, with the per-node analysis when it has been run."""
    from fastapi.responses import Response
    from app.services import profile_doc, store
    from app.services.profile import comprehend
    from app.services.profile.assemble import build_profile
    g = _snapshot(source, ws, project, snap)
    comp = store.peek(comprehend.comprehend_key(source, ws, project, snap))
    return Response(profile_doc.build_zip(build_profile(g, comp=comp)), media_type="application/zip",
                    headers={"Content-Disposition": _attachment("token-profile-%s.zip" % project)})
