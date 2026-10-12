"""Token Auditor FastAPI app: the validation API (/api/validations), the UI's JSON (/api/suite), the React UI (/app).

Run:  python -m app.web.server   (http://127.0.0.1:8100, live reload)
Build the UI with `npm ci && npm run build` in web/.
"""
import os

import contextlib
import threading

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse

_HERE = os.path.dirname(os.path.abspath(__file__))

@contextlib.asynccontextmanager
async def _lifespan(app):
    """Run VALIDATION_INPROCESS_WORKERS validation workers in the web process: 2 by default, 1 on Cloud Run."""
    import credentials
    from validation import worker
    stop = threading.Event()
    worker.start(int(credentials.get_config("VALIDATION_INPROCESS_WORKERS", "2")), stop)
    yield
    stop.set()
    worker.release_all()


app = FastAPI(title="Token Auditor", lifespan=_lifespan)

from app.web import cache, callsites, model_audits, profiles, suite, validations  # noqa: E402  the APIs and the downloads
app.include_router(validations.api)
app.include_router(suite.api)
app.include_router(cache.api)
app.include_router(profiles.api)
app.include_router(callsites.api)
app.include_router(model_audits.api)


def _openapi():
    """/openapi.json and /docs describe the validation API only."""
    if app.openapi_schema is None:
        from fastapi.openapi.utils import get_openapi
        app.openapi_schema = get_openapi(title="Token Auditor validation API", version="1",
                                         description=validations.API_DOC,
                                         routes=validations.api.routes)
    return app.openapi_schema


app.openapi = _openapi

_DIST = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "web", "dist")


@app.get("/app", include_in_schema=False)
def web_root():
    return RedirectResponse("/app/")


@app.get("/app/{path:path}", include_in_schema=False)
def web_ui(path: str):
    """The built React UI: its files, and index.html for every page route inside it."""
    f = os.path.realpath(os.path.join(_DIST, path))
    if path and f.startswith(os.path.realpath(_DIST) + os.sep) and os.path.isfile(f):
        return FileResponse(f, headers={"Cache-Control": "public, max-age=31536000, immutable"
                                        if path.startswith("assets/") else "no-cache"})
    index = os.path.join(_DIST, "index.html")
    if not os.path.isfile(index):
        return HTMLResponse("The web UI is not built: run <code>npm ci &amp;&amp; npm run build</code> in web/.", 503)
    return FileResponse(index, headers={"Cache-Control": "no-cache"})



@app.get("/healthz/db")
def healthz_db():
    """Liveness and the store in use ('postgresql' or 'sqlite', host/db without credentials); 503 when unreachable."""
    from app.services import db
    h = db.health()
    return JSONResponse(h, status_code=200 if h.get("ok") else 503)


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/app/")


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8100"))
    uvicorn.run("app.web.server:app", host=host, port=port, reload=True)
