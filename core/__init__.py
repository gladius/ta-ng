"""Core: what every module (cache, models, router) is built on. identity = call-site grouping; ledger = per-call-site
aggregates from the log in BigQuery. Modules depend on core, never on each other."""
