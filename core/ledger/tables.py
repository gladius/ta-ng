"""The ledger's tables in the app's database, filled by each refresh; every page reads these, never BigQuery.

  usecase_lines   per usecase, the stable lines (the router's fingerprint source)
  ledger_daily    usecase x site x model x day: tokens, cache reads and writes, cost
  cache_gaps      per site, model and day: calls by time since the previous call sharing the prefix
  cache_diagnoses per site and model: the cache module's cause, suggestion, lifetime what-if and story
"""
import datetime
import json

from sqlalchemy import Boolean, Column, Date, DateTime, Float, Integer, String, Table, Text, delete, select

from app.services import db

usecase_lines = Table(
    "usecase_lines", db.metadata,
    Column("usecase", Text, primary_key=True, nullable=False, default=""),
    Column("line", Text, primary_key=True),
    Column("share", Float, nullable=False),
    Column("limited_days", Boolean, nullable=False),
)
ledger_daily = Table(
    "ledger_daily", db.metadata,
    Column("day", Date, primary_key=True),
    Column("usecase", Text, primary_key=True, default=""),
    Column("site", String(32), primary_key=True),
    Column("model", Text, primary_key=True, default=""),
    Column("calls", Integer, nullable=False),
    Column("prompt_tokens", Integer), Column("cached_tokens", Integer), Column("written_tokens", Integer),
    Column("completion_tokens", Integer), Column("calls_cached", Integer),
    Column("cost", Float), Column("cost_nocache", Float),
    Column("avg_ms", Float), Column("avg_messages", Float), Column("calls_with_tools", Integer),
)
cache_gaps = Table(
    "cache_gaps", db.metadata,
    Column("day", Date, primary_key=True),
    Column("usecase", Text, primary_key=True, default=""),
    Column("site", String(32), primary_key=True),
    Column("model", Text, primary_key=True, default=""),
    Column("bucket", String(8), primary_key=True),
    Column("calls", Integer, nullable=False),
    Column("prefix_tokens", Integer), Column("prompt_tokens", Integer), Column("cached", Integer), Column("written", Integer),
)
cache_diagnoses = Table(
    "cache_diagnoses", db.metadata,
    Column("site", String(32), primary_key=True),
    Column("model", Text, primary_key=True, default=""),
    Column("usecase", Text),
    Column("cause", String(24)),
    Column("saving", Float),
    Column("proof", String(8)),
    Column("diagnosis", Text),        # JSON: the full diagnosis (evidence, suggestion, achievable share)
    Column("ttl", Text),              # JSON: the lifetime what-if, or null
    Column("story", Text),            # JSON: now / why / action, for the site (same for every model)
    Column("built_at", DateTime, nullable=False),
)

_NUMS = ("calls", "prompt_tokens", "cached_tokens", "written_tokens", "completion_tokens", "calls_cached", "cost",
         "cost_nocache", "avg_ms", "avg_messages", "calls_with_tools")


def replace(table, rows, key_cols=()):
    """Replace the table's content (or the rows matching `key_cols` values present in `rows`) with `rows`."""
    eng = db.ready()
    with eng.begin() as c:
        c.execute(delete(table))
        for i in range(0, len(rows), 500):
            c.execute(table.insert(), rows[i:i + 500])
    return len(rows)


def _usecase(v):
    return v if v is not None else ""


def store_usecase_lines(rows):
    return replace(usecase_lines, [{"usecase": _usecase(r["usecase"]), "line": r["line"], "share": float(r["share"]),
                                    "limited_days": bool(r["limited_days"])} for r in rows])


def store_ledger(rows):
    return replace(ledger_daily, [{"day": r["day"], "usecase": _usecase(r["usecase"]), "site": str(r["site"]),
                                   "model": r["model"] or "", **{k: r.get(k) for k in _NUMS}} for r in rows])


def store_gaps(rows):
    return replace(cache_gaps, [{"day": r["day"], "usecase": _usecase(r["usecase"]), "site": str(r["site"]),
                                 "model": r["model"] or "", "bucket": r["bucket"], "calls": r["calls"],
                                 "prefix_tokens": r.get("prefix_tokens"), "prompt_tokens": r.get("prompt_tokens"),
                                 "cached": r.get("cached"), "written": r.get("written")} for r in rows])


def store_diagnoses(rows, built_at):
    return replace(cache_diagnoses, [{"site": str(r["site"]), "model": r["model"] or "", "usecase": _usecase(r.get("usecase")),
                                      "cause": r["cause"], "saving": r["saving"], "proof": r["proof"],
                                      "diagnosis": json.dumps(r["diagnosis"]), "ttl": json.dumps(r["ttl"]),
                                      "story": json.dumps(r["story"]), "built_at": built_at} for r in rows])


def rows(table, where=None, order=None):
    eng = db.ready()
    stmt = select(*table.columns)
    if where is not None:
        stmt = stmt.where(where)
    if order is not None:
        stmt = stmt.order_by(order)
    with eng.connect() as c:
        out = [dict(zip([col.name for col in table.columns], r)) for r in c.execute(stmt).all()]
    for r in out:
        for k in ("diagnosis", "ttl", "story"):
            if k in r and isinstance(r[k], str):
                r[k] = json.loads(r[k])
        if "day" in r and isinstance(r["day"], (datetime.date, datetime.datetime)):
            r["day"] = str(r["day"])[:10]
        if "built_at" in r and r["built_at"]:
            r["built_at"] = r["built_at"].isoformat(timespec="seconds")
    return out
