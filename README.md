# Token Auditor

Audits the AI agents behind a LiteLLM gateway by reading its BigQuery log. It finds the call-sites in the log (the log
has no call-site id), measures their cost and cache use, and checks whether another model can do each call-site's job.
[docs/model-validation.html](docs/model-validation.html) explains the idea; [backend/FLOW.md](backend/FLOW.md) explains
how a day of the log is processed.

## Layout

| Folder | What it is |
|---|---|
| `web/` | The UI: React, TypeScript, Vite. In production nginx serves it and forwards API calls to the backend |
| `backend/` | The API and the validation workers: Python, FastAPI. All Python commands run from this folder |
| `docs/` | Explanations and findings |

Inside `backend/`:

| Folder | What it is |
|---|---|
| `app/` | The web app, its routes and shared services |
| `core/` | Reading the log, finding call-sites, tasks and schedules |
| `modules/` | Model audits, cache, quality checks |
| `validation/` | The engine that re-runs requests and judges the answers |
| `connectors/`, `contract/` | Reading logged requests into one format |
| `config/` | The model catalog and database migrations |
| `tools/` | Scripts run by hand, some of them paid |
| `tests/` | Offline, free, about 2 seconds |

## Run it locally

```bash
cd backend
python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt
# create backend/.env with the settings below, plus APP_ENV=dev to use SQLite
.venv/bin/python -m uvicorn app.web.server:app --host 127.0.0.1 --port 8100

cd ../web
npm ci
npm run dev       # the UI with instant reload; passes /api to port 8100
```

Or run `npm run build` once: the local backend then serves the UI at http://127.0.0.1:8100/app/.

## Test

```bash
cd backend
env ANTHROPIC_API_KEY= AUDIT_DEBUG= .venv/bin/python -m pytest -q tests
```

## Images

Two images, each built from its own folder. `docker` works the same as `podman` here.

```bash
podman build -t token-auditor-backend backend
podman build -t token-auditor-web web
```

| Image | Listens on | Needs when it runs |
|---|---|---|
| `token-auditor-backend` | `$PORT`, default 8080 | `DATABASE_URL`, the gateway and BigQuery settings below. It stops at startup without `DATABASE_URL` |
| `token-auditor-web` | 8080 | `BACKEND_URL`, e.g. `http://token-auditor-backend:8080` |

Send outside traffic to the web image. It serves `/app` and forwards `/api`, `/docs` and `/openapi.json` to the backend.
Health checks: `/healthz` on the web image, `/healthz/db` on the backend.

**The API has no sign-in.** Anyone who can reach either image can read the audits and start paid ones, so the deployment
has to control who can reach them.

Run one backend worker per deployment (the image sets `VALIDATION_INPROCESS_WORKERS=1`) and keep its CPU always
available: if a worker's heartbeat stalls, another instance redoes and pays for the same work again. The backend needs
about 5 seconds to release held work on shutdown.

## Settings

The backend reads process environment first, then `backend/.env` (gitignored, never in an image).

| Setting | Required | What it does |
|---|---|---|
| `DATABASE_URL` | Yes, unless `APP_ENV=dev` | Postgres, e.g. `postgresql+psycopg://...`. Tables are created on first use; a changed column needs a manual `ALTER TABLE` |
| `APP_ENV` | No | `dev` allows SQLite at `backend/.data/audit_store.db`. Leave it unset in production |
| `LLM_GATEWAY_URL`, `LLM_GATEWAY_KEY` | Yes | Every model call goes through the gateway |
| `BQ_TABLE`, `BQ_REQUEST_COLUMN`, `BQ_RESPONSE_COLUMN`, `BQ_ID_COLUMN`, `BQ_USECASE_COLUMN`, `BQ_TIME_COLUMN`, `BQ_LATENCY_COLUMN` | Yes | Where the log is. A missing one stops with its name |
| `BQ_PARAMS_COLUMN`, `BQ_MODEL_COLUMN` | No | Extra log columns |
| `SKIP_USECASES`, `ONLY_USECASES` | No | Comma-separated LIKE patterns: usecases never read, or the only ones read |
| `ANALYSIS_WINDOW_DAYS` | No, default 7 | Days of the log kept and processed |
| `BQ_MAX_TB` | No, default 2 | A BigQuery statement that would read more is refused before it runs |
| `AUDIT_PROFILE_MODEL` | No, default `claude-sonnet-5` | Names and describes each call-site |
| `AUDIT_JUDGE_MODEL` | No, default `claude-sonnet-5` | Judges the answers in a model audit |

Nothing processes by itself until the `process_days` schedule is turned on in the UI.

## Costs

- Reading a day of the log bills the whole day in BigQuery, whatever the usecase filter.
- Model audits and call-site naming pay for model calls. An audit's `max_budget_usd` caps it; there is no cap unless you
  set one.
- Keep the gateway key's max budget unset: LiteLLM charges each call's worst case against it and never settles it for
  our calls, so it trips long before the real spend.
