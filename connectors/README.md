# Connectors — observability trace ingestion

Pluggable **adapters** that turn recorded platform traces into Token Auditor traces
(written to `corpus/data/*.json`, which `run_auditor.py` already reads). LangSmith and
Galileo ship today; other platforms drop in behind the same interface.

## Use

```bash
# Offline (no key, no install) — replay an exported run JSON:
python -m connectors langsmith --offline connectors/langsmith/sample_runs.json

# Live — pull recent LLM runs from a LangSmith project (needs LANGSMITH_API_KEY in env):
python -m connectors langsmith --project my-proj --since-hours 168 --limit 100

# Then audit one discovered call-site:
python run_auditor.py "support-agent/triage"
```

The pull prints every discovered **call-site** (one bucket per `agent_id`) with its sample
count, and flags any models missing from the catalog.

## How grouping works

A **bucket** = all samples of ONE call-site; the auditor optimizes one call-site per bucket
(it picks the heaviest sample and uses the rest for cross-sample signal, e.g. the stable
cacheable prefix). So each node should be its own `agent_id`. Default call-site key:

    "<agent>/<node>"   agent = metadata.agent_id | metadata.agent | project
                       node  = metadata.langgraph_node | metadata.node | run.name

Override with `--group-by run_name | langgraph_node | metadata:<key>`.

## Model pricing

Real model names are mapped onto the catalog in [`auditor/models.json`](../auditor/models.json)
(providers → models in capability order, with prices). Unknown models are surfaced in the pull
summary — add them there. Model-swap downgrades one step down the same provider's list.

## Adding an adapter

A connector is a **subpackage** `connectors/<name>/` exporting `ADAPTER = <Adapter subclass>()`. The
registry ([`registry.py`](registry.py)) **auto-discovers** it — there is no central file to edit (plain
modules are ignored; only packages register).

1. Create `connectors/<name>/__init__.py` re-exporting `ADAPTER`, and `connectors/<name>/adapter.py`
   with an `Adapter` subclass implementing:
   - `fetch(**opts)` → iterable of raw records (support `records=<dicts/path>` for an offline mode),
   - `to_trace(record, **opts)` → `schema.build_trace(...)` (or `None` to skip non-LLM records).
2. Co-locate a `sample_runs.json` + `test_adapter.py` so the mapping is proven offline (no key, no SDK).

Shared normalization (messages, tools, output, usage, model mapping) lives in
[`schema.py`](schema.py) — reuse it so every platform emits identical trace shapes.

Shipped adapters: **`langsmith`** and **`galileo`** — use either as a reference. The Galileo adapter
([`galileo/adapter.py`](galileo/adapter.py)) pulls LLM spans via `POST /v2/projects/{id}/spans/search`;
set `GALILEO_API_KEY` and pass `project_id` (or `project` name) — see its module docstring for the two
live-path details to confirm against the OpenAPI spec before a first production run.
