# Token Auditor — notes for Claude

## Starting and stopping (local)
Two systemd user services run everything. They start at login and restart themselves. Never start the app or the
tunnel with ad-hoc background shell commands; they die with the session.

| Service | What it runs |
|---|---|
| `litellm-tunnel` | SSH tunnel to the LiteLLM gateway on Hetzner (`root@95.217.11.175`) through `~/Workspace/trust_router/deploy/lt tunnel`; the gateway is then at `http://localhost:4000`. Back within ~5 s after a drop |
| `token-auditor` | The web app plus 2 in-process validation workers at `http://127.0.0.1:8100` |

```bash
systemctl --user status litellm-tunnel token-auditor
systemctl --user restart token-auditor          # after every code change
journalctl --user -u token-auditor -f           # app logs
curl -s localhost:4000/health/liveliness        # gateway reachable?
```
Unit files: `~/.config/systemd/user/litellm-tunnel.service`, `~/.config/systemd/user/token-auditor.service`.

## Settings (`.env`, gitignored)
- `LLM_GATEWAY_URL`, `LLM_GATEWAY_KEY`: every model call goes through the gateway (key alias `token-auditor`).
  Remove both to call Anthropic directly with `ANTHROPIC_API_KEY`.
- `VALIDATION_API_KEY`: the `X-API-Key` header for `/api/validations`. A header, never a URL parameter.
- `BQ_TABLE`, `BQ_REQUEST_COLUMN`, `BQ_RESPONSE_COLUMN`: the BigQuery source; it is hidden unless all three are set.
- `BQ_ID_COLUMN` (default `request_id`), `BQ_PARAMS_COLUMN`, `BQ_MODEL_COLUMN`: for validations sent by `request_id`.
  trust_router's view needs `BQ_PARAMS_COLUMN=model_parameters` (max_tokens, tools) and `BQ_MODEL_COLUMN=model`
  (its responses name `jev-router`, not the model that answered).
- `DATABASE_URL`: unset means SQLite at `.data/audit_store.db` (dev); Postgres in production, no code change.

## Tests
```bash
env ANTHROPIC_API_KEY= LANGSMITH_KEY= LANGSMITH_API_KEY= LANGCHAIN_API_KEY= AUDIT_DEBUG= .venv/bin/python -m pytest -q tests
```
Offline and free (~2 s). `tests/conftest.py` keeps every test off the gateway even though `.env` points at it.

## Production (Cloud Run)
- One validation worker per deployment: `VALIDATION_INPROCESS_WORKERS=1` (default 2 is for local). Background threads
  need `--no-cpu-throttling --min-instances 1`, or a heartbeat stalls and another instance redoes (re-pays) the group.
- Start: `uvicorn app.web.server:app --host 0.0.0.0 --port $PORT --timeout-graceful-shutdown 5` (releases held groups
  within Cloud Run's 10 s SIGTERM grace). `DATABASE_URL` must be set there; the app refuses to fall back to SQLite.
- Keep `.env`, `.data/`, `.audit_debug/` out of the image; leave `AUDIT_DEBUG` and `LANGSMITH_*` unset.
- Tables are created on first use; a column change on an existing table needs a manual `ALTER TABLE` (no migrations).

## Real (paid) runs
- Full end-to-end check (2 instances, crash + redeploy, request_id items; about $1):
  `E2E_DATABASE_URL=postgresql+psycopg://... .venv/bin/python tools/validation_e2e.py --reset` (--reset drops the
  validation tables of THAT database first).
- Say the cost estimate first and always set `max_budget_usd` on the job.
- Submit to the running app on 8100. A cheap judge for plumbing checks is `gpt-5.4-mini`; real verdicts use
  `claude-sonnet-5`.

## The gateway is shared production
- Adding models or keys on it needs the user's OK. It serves gpt-5, gpt-5.4, gpt-5.4-mini, gpt-5.4-nano, gpt-5.5,
  jev-router, claude-sonnet-5, claude-opus-5, claude-haiku-4-5.
- Our calls send LiteLLM's no-cache and no-log options: re-runs are fresh and stay out of BigQuery.
- Keep the `token-auditor` key's max budget UNSET. LiteLLM v1.103.0 pre-charges each call's worst case (from
  max_tokens) against a key budget and never settles it for our no-log calls, so a $25 cap trips after ~17 calls of
  100k max_tokens or ~1000 judge calls, whatever they really cost (BerriAI/litellm #27639, #30460, #27735). It can't
  track real spend anyway: no-log calls leave the key's spend at $0. Each job's `max_budget_usd` is the real cap.
