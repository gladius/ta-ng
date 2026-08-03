# Token Auditor

Out-of-path, advisory token-waste auditor. It reads **recorded** agent traces (LangSmith / Galileo),
detects per-call-site waste, and proves each fix preserves behavior — it never sits in the live path.

This is a clean rebuild, grown one **vertical slice** at a time. Nothing speculative: a component exists
only when a flow needs it.

## Status — Slice 1

Navigation only: **data source → workspaces → agents**. Three read-only pages, nothing else.

```
token_auditor/
  contract/        # the trace shape (copied as-is)
  connectors/      # langsmith + galileo adapters + datasource navigation (copied as-is)
  auditor/         # util.py + models.json — the model/price catalog (contract canonicalizes model names against it)
  credentials.py   # the .env / process-env credential loader (copied as-is)
  app/
    services/nav.py        # the one seam: web -> connectors
    web/server.py          # FastAPI, 3 routes
    web/templates/         # base + datasource + workspaces + agents
    web/static/app.css
```

## Run

```bash
pip install fastapi "uvicorn[standard]" jinja2 langsmith
python -m app.web.server            # http://127.0.0.1:8100
```

To see real workspaces/agents, provide credentials (LangSmith shown; Galileo analogous). Create a
gitignored `.env` at the project root:

```
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_ENDPOINT=https://api.smith.langchain.com      # or your region's endpoint
LANGSMITH_WORKSPACE_ID=...                               # org-scoped keys need this (sent as X-Tenant-Id)
```

Without credentials the pages still render — sources show **no credentials** and lists are empty.

## Principle

Copy tested logic in; never import from another project. Add shared infrastructure only when a **second**
flow actually needs it. One flow finished and trusted before the next.
